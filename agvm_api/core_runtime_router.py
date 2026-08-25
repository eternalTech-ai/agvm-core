# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from brain_registry import active_brain_summary
from edition_gate import build_edition_route_report
from setup_env import ProviderKeyTestError, managed_env_status, save_managed_env_values, test_openai_provider_key


HostedRegistrySummaryProvider = Callable[[], dict[str, Any]]


class SetupEnvSaveRequest(BaseModel):
    openai_api_key: str | None = Field(default=None, max_length=4096)
    agvm_llm_enabled: bool | None = True
    agvm_default_brain_id: str | None = Field(default=None, max_length=160)
    agvm_llm_model: str | None = Field(default=None, max_length=120)
    agvm_compiler_model: str | None = Field(default=None, max_length=120)
    agvm_retrieval_model: str | None = Field(default=None, max_length=120)
    agvm_answer_model: str | None = Field(default=None, max_length=120)
    agvm_sleep_model: str | None = Field(default=None, max_length=120)
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
            return await run_in_threadpool(test_openai_provider_key, payload["api_key"])
        except ProviderKeyTestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.response_detail()) from None

    @router.get("/health")
    def health() -> dict[str, Any]:
        summary = active_brain_summary()
        hosted_summary = _hosted_registry_summary(hosted_registry_summary_provider)
        return {
            "ok": True,
            "service": app_name,
            "version": app_version,
            "active_brain_id": summary.get("brain_id"),
            "brain_registry_ready": bool(summary.get("brain_id")),
            "hosted_tenant_registry_ready": bool((hosted_summary.get("validation") or {}).get("passed")),
            "hosted_tenant_registry": hosted_summary,
            "runtime_scope_status": summary.get("runtime_scope_status"),
        }

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
