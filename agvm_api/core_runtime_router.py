# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from brain_registry import active_brain_summary
from edition_gate import build_edition_route_report
from llm import clear_llm_provider_auth_errors, llm_runtime_status
from setup_env import ProviderKeyTestError, managed_env_status, save_managed_env_values, test_openai_provider_key
from release_provenance import runtime_release_provenance


HostedRegistrySummaryProvider = Callable[[], dict[str, Any]]


def _runtime_provider_execution_block() -> dict[str, Any]:
    """Return recent observed provider execution failures that make AI unusable.

    Configuration only proves that a credential is present. Search/Grow need a
    provider execution to succeed. A key with exhausted quota must not keep the
    runtime in an AI-ready state after the process has observed that failure.
    """

    try:
        runtime = llm_runtime_status()
    except Exception:  # pragma: no cover - readiness must not break liveness.
        return {}
    blocked_roles: list[dict[str, Any]] = []
    for role, row in dict(runtime or {}).items():
        if not isinstance(row, dict):
            continue
        error = str(row.get("last_error") or "").strip()
        if not error:
            continue
        lowered = error.lower()
        if any(
            marker in lowered
            for marker in (
                "insufficient_quota",
                "credit_balance",
                "no credits remaining",
                "billing",
                "invalid_api_key",
                "401",
                "403",
            )
        ):
            blocked_roles.append(
                {
                    "role": str(role),
                    "last_error_type": (
                        "quota_exhausted"
                        if any(marker in lowered for marker in ("insufficient_quota", "credit_balance", "no credits remaining", "billing"))
                        else "provider_auth_rejected"
                    ),
                    "last_model": row.get("last_model") or None,
                    "requests": int(row.get("requests") or 0),
                    "success": int(row.get("success") or 0),
                }
            )
    if not blocked_roles:
        return {}
    primary = blocked_roles[0]
    return {
        "state": primary["last_error_type"],
        "blocked": True,
        "roles": blocked_roles,
    }


def runtime_configuration_status() -> dict[str, Any]:
    """Describe provider readiness without turning missing setup into failed liveness."""

    setup = managed_env_status()
    provider = dict(setup.get("provider") or {})
    llm = dict(setup.get("llm") or {})
    edition = str(os.getenv("AGVM_EDITION") or "local").strip().lower() or "local"
    request_scoped_provider = edition == "cloud"
    llm_enabled = bool(llm.get("enabled", True))
    provider_configured = bool(provider.get("configured"))
    provider_execution = _runtime_provider_execution_block()
    ai_ready = bool(llm_enabled and (provider_configured or request_scoped_provider))
    if ai_ready and provider_execution.get("blocked"):
        ai_ready = False
        state = str(provider_execution.get("state") or "provider_execution_blocked")
    elif ai_ready:
        state = "request_scoped_provider" if request_scoped_provider and not provider_configured else "ready"
    elif not llm_enabled:
        state = "ai_disabled"
    else:
        state = "configuration_required"
    return {
        "schema_version": "agvm.runtime_configuration.v1",
        "state": state,
        "edition": edition,
        "service_live": True,
        "ai_ready": ai_ready,
        "needs_configuration": state == "configuration_required",
        "provider": {
            "name": "openai",
            "configured": provider_configured,
            "source": provider.get("source") or "missing",
            "credential_mode": "request_scoped" if request_scoped_provider else "local_managed_or_process_env",
            "execution": provider_execution or {"state": "not_observed", "blocked": False},
        },
        "llm": {
            "enabled": llm_enabled,
            "model": llm.get("model"),
            "compiler_model": llm.get("compiler_model"),
            "retrieval_model": llm.get("retrieval_model"),
            "answer_model": llm.get("answer_model"),
            "sleep_model": llm.get("sleep_model"),
            "planner_model": llm.get("planner_model"),
            "ai_spatial_model": llm.get("ai_spatial_model"),
            "branch_controller_model": llm.get("branch_controller_model"),
            "evidence_judge_model": llm.get("evidence_judge_model"),
            "master_model": llm.get("master_model"),
            "grow_semantic_model": llm.get("grow_semantic_model"),
        },
        "setup": {
            "status_path": "/setup/env",
            "save_path": "/setup/env",
            "provider_test_path": "/setup/provider/test",
        },
    }


class SetupEnvSaveRequest(BaseModel):
    openai_api_key: str | None = Field(default=None, max_length=4096)
    agvm_llm_enabled: bool | None = True
    agvm_default_brain_id: str | None = Field(default=None, max_length=160)
    agvm_llm_model: str | None = Field(default=None, max_length=120)
    agvm_compiler_model: str | None = Field(default=None, max_length=120)
    agvm_retrieval_model: str | None = Field(default=None, max_length=120)
    agvm_answer_model: str | None = Field(default=None, max_length=120)
    agvm_sleep_model: str | None = Field(default=None, max_length=120)
    agvm_planner_model: str | None = Field(default=None, max_length=120)
    agvm_ai_spatial_model: str | None = Field(default=None, max_length=120)
    agvm_branch_controller_model: str | None = Field(default=None, max_length=120)
    agvm_evidence_judge_model: str | None = Field(default=None, max_length=120)
    agvm_master_model: str | None = Field(default=None, max_length=120)
    agvm_grow_semantic_model: str | None = Field(default=None, max_length=120)
    agvm_clone_app_arbiter_model: str | None = Field(default=None, max_length=120)
    agvm_clone_app_sufficiency_model: str | None = Field(default=None, max_length=120)
    agvm_clone_app_speaker_model: str | None = Field(default=None, max_length=120)
    agvm_clone_app_prefetch_model: str | None = Field(default=None, max_length=120)
    agvm_clone_app_teach_model: str | None = Field(default=None, max_length=120)


def create_core_runtime_router(
    *,
    app_name: str,
    app_version: str,
    hosted_registry_summary_provider: HostedRegistrySummaryProvider | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/setup/env")
    def setup_env_status() -> dict[str, Any]:
        return managed_env_status()

    @router.post("/setup/env")
    def setup_env_save(payload: SetupEnvSaveRequest) -> dict[str, Any]:
        updates = _setup_env_updates_from_payload(payload)
        if not updates:
            raise HTTPException(status_code=400, detail="no_supported_env_values")
        try:
            return save_managed_env_values(updates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/setup/provider/test")
    async def setup_provider_key_test(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - return a fixed error without reflecting request content.
            raise HTTPException(status_code=400, detail=_provider_test_request_error("invalid_json")) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("api_key"), str):
            raise HTTPException(status_code=400, detail=_provider_test_request_error("provider_key_required"))
        try:
            result = await run_in_threadpool(test_openai_provider_key, payload["api_key"])
            if bool(result.get("ok")):
                result["cleared_provider_auth_errors"] = clear_llm_provider_auth_errors()
            return result
        except ProviderKeyTestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.response_detail()) from None

    @router.get("/health")
    def health(response: Response) -> dict[str, Any]:
        summary = active_brain_summary()
        hosted_summary = _hosted_registry_summary(hosted_registry_summary_provider)
        configuration = runtime_configuration_status()
        release = runtime_release_provenance(component="core_api", default_version=app_version)
        release_status = release["release_bundle_status"]
        if not release_status["ok"]:
            response.status_code = 503
        return {
            "ok": bool(release_status["ok"]),
            "code": release_status["code"],
            "service": app_name,
            "version": release["version"],
            "revision": release["revision"],
            "source_sha": release["source_sha"],
            "release_bundle": release["release_bundle"],
            "release_bundle_status": release_status,
            "release": release,
            "ai_ready": configuration["ai_ready"],
            "needs_configuration": configuration["needs_configuration"],
            "runtime_configuration": configuration,
            "active_brain_id": summary.get("brain_id"),
            "brain_registry_ready": bool(summary.get("brain_id")),
            "hosted_tenant_registry_ready": bool((hosted_summary.get("validation") or {}).get("passed")),
            "hosted_tenant_registry": hosted_summary,
            "runtime_scope_status": summary.get("runtime_scope_status"),
        }

    @router.get("/version")
    def version() -> dict[str, Any]:
        return runtime_release_provenance(component="core_api", default_version=app_version)

    @router.get("/runtime/edition")
    def runtime_edition_endpoint(request: Request) -> dict[str, Any]:
        return build_edition_route_report(request.app)

    return router


def _hosted_registry_summary(provider: HostedRegistrySummaryProvider | None) -> dict[str, Any]:
    if provider is None:
        return {
            "schema_version": "agvm.hosted_tenant_registry_summary.v1",
            "validation": {"passed": False, "reason": "hosted_registry_not_configured"},
        }
    try:
        return dict(provider() or {})
    except Exception as exc:  # noqa: BLE001 - health must remain diagnostic.
        return {
            "schema_version": "agvm.hosted_tenant_registry_summary.v1",
            "validation": {"passed": False, "reason": "hosted_registry_unavailable", "error": str(exc)},
        }


def _provider_test_request_error(code: str) -> dict[str, Any]:
    return {
        "schema_version": "agvm.provider_key_test.v1",
        "ok": False,
        "provider": "openai",
        "status": "invalid_request",
        "capability": "model_access",
        "persisted": False,
        "error": {
            "code": code,
            "message": "Enter a provider key before testing.",
            "retryable": False,
        },
    }


def _setup_env_value(name: str, value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "\n" in text or "\r" in text:
        raise HTTPException(status_code=400, detail=f"{name}_must_be_single_line")
    return text


def _setup_env_updates_from_payload(payload: SetupEnvSaveRequest) -> dict[str, str]:
    field_map = {
        "openai_api_key": "OPENAI_API_KEY",
        "agvm_default_brain_id": "AGVM_DEFAULT_BRAIN_ID",
        "agvm_llm_model": "AGVM_LLM_MODEL",
        "agvm_compiler_model": "AGVM_COMPILER_MODEL",
        "agvm_retrieval_model": "AGVM_RETRIEVAL_MODEL",
        "agvm_answer_model": "AGVM_ANSWER_MODEL",
        "agvm_sleep_model": "AGVM_SLEEP_MODEL",
        "agvm_planner_model": "AGVM_PLANNER_MODEL",
        "agvm_ai_spatial_model": "AGVM_AI_SPATIAL_MODEL",
        "agvm_branch_controller_model": "AGVM_BRANCH_CONTROLLER_MODEL",
        "agvm_evidence_judge_model": "AGVM_EVIDENCE_JUDGE_MODEL",
        "agvm_master_model": "AGVM_MASTER_MODEL",
        "agvm_grow_semantic_model": "AGVM_GROW_SEMANTIC_MODEL",
        "agvm_clone_app_arbiter_model": "AGVM_CLONE_APP_ARBITER_MODEL",
        "agvm_clone_app_sufficiency_model": "AGVM_CLONE_APP_SUFFICIENCY_MODEL",
        "agvm_clone_app_speaker_model": "AGVM_CLONE_APP_SPEAKER_MODEL",
        "agvm_clone_app_prefetch_model": "AGVM_CLONE_APP_PREFETCH_MODEL",
        "agvm_clone_app_teach_model": "AGVM_CLONE_APP_TEACH_MODEL",
    }
    updates: dict[str, str] = {}
    for field_name, env_name in field_map.items():
        value = _setup_env_value(field_name, getattr(payload, field_name))
        if value is not None:
            updates[env_name] = value
    if payload.agvm_llm_enabled is False:
        raise HTTPException(status_code=400, detail="agvm_llm_enabled_cannot_be_false")
    if payload.agvm_llm_enabled is not None:
        updates["AGVM_LLM_ENABLED"] = "true" if payload.agvm_llm_enabled else "false"
    return updates
