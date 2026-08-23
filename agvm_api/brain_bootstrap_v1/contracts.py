# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any


SESSION_SCHEMA_VERSION = "agvm.brain_bootstrap_v1.session_revision.v1"
RESPONSE_SCHEMA_VERSION = "agvm.brain_bootstrap_v1.response.v1"
ACTION_CONTRACT_SCHEMA_VERSION = "agvm.brain_bootstrap_v1.cloud_action_contract.v1"

OPERATIONS = (
    "start",
    "status",
    "answer",
    "add_source",
    "preview",
    "apply",
    "resume",
    "recover",
    "cancel",
)

CLOUD_REQUIRED_CAPABILITIES = frozenset({"ai_research", "fitting", "backfill", "activation"})
LOCAL_CAPABILITIES = frozenset({"manual_interview", "manual_source", "grow_review"})


def build_cloud_action_contract(*, operation: str, capability: str, brain_id: str, session_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": ACTION_CONTRACT_SCHEMA_VERSION,
        "state": "cloud_execution_required",
        "operation": operation,
        "capability": capability,
        "brain_id": brain_id,
        "session_id": session_id,
        "execution_surface": "detwin_cloud",
        "requires_account": True,
        "requires_entitlement": True,
        "requires_credits": True,
        "requires_usage_preflight": True,
        "requires_dynamic_usage_settlement": True,
        "local_execution_allowed": False,
        "local_session_mutated": False,
        "recovery": (
            "Open Detwin Cloud with an authenticated account, then run the requested Bootstrap capability "
            "against this brain and session. Local Core does not simulate paid AI execution."
        ),
    }


def bootstrap_response(
    *,
    operation: str,
    status: str,
    brain_id: str,
    session: dict[str, Any] | None = None,
    action_contract: dict[str, Any] | None = None,
    replayed: bool = False,
) -> dict[str, Any]:
    revision = int((session or {}).get("revision") or 0)
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "operation": operation,
        "status": status,
        "brain_id": brain_id,
        "session_id": (session or {}).get("session_id"),
        "revision": revision,
        "revision_digest": (session or {}).get("revision_digest"),
        "lifecycle_state": (session or {}).get("lifecycle_state"),
        "session": session,
        "action_contract": action_contract,
        "idempotent_replay": replayed,
        "mutation_contract": {
            "answers_mutate_brain": False,
            "sources_mutate_brain": False,
            "preview_mutates_brain": False,
            "apply_requires_explicit_confirmation": True,
            "apply_is_the_only_brain_write_boundary": True,
        },
    }
