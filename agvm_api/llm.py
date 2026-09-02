# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import hashlib
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from config import (
    DEFAULT_ANSWER_MODEL,
    DEFAULT_AI_SPATIAL_MODEL,
    DEFAULT_BRANCH_CONTROLLER_MODEL,
    DEFAULT_COMPILER_MODEL,
    DEFAULT_EVIDENCE_JUDGE_MODEL,
    DEFAULT_GROW_SEMANTIC_MODEL,
    DEFAULT_MASTER_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_PLANNER_MODEL,
    DEFAULT_RETRIEVAL_MODEL,
    DEFAULT_SLEEP_MODEL,
)

_ROLE_NAMES = (
    "compiler",
    "planner",
    "ai_spatial",
    "retrieval",
    "branch_controller",
    "evidence_judge",
    "master",
    "answer",
    "sleep",
    "grow_semantic",
    "clone_arbiter",
    "clone_sufficiency",
    "clone_speaker",
    "clone_prefetch",
    "context_correction",
)
_LLM_RUNTIME: dict[str, Any] = {
    "requests": {role: 0 for role in _ROLE_NAMES},
    "success": {role: 0 for role in _ROLE_NAMES},
    "fallback": {role: 0 for role in _ROLE_NAMES},
    "last_path": {role: "fallback" for role in _ROLE_NAMES},
    "last_error": {role: None for role in _ROLE_NAMES},
    "last_queue_wait_ms": {role: 0.0 for role in _ROLE_NAMES},
    "last_model": {role: "" for role in _ROLE_NAMES},
    "last_timeout_seconds": {role: 0.0 for role in _ROLE_NAMES},
    "queued": {role: 0 for role in _ROLE_NAMES},
}
_LLM_PROVIDER_LOCK = threading.Lock()
_LLM_PROVIDER_SEMAPHORE: threading.BoundedSemaphore | None = None
_LLM_PROVIDER_SEMAPHORE_LIMIT: int | None = None


_PROVIDER_BLOCKING_ERROR_MARKERS = (
    "insufficient_quota",
    "credit_balance",
    "no credits remaining",
    "billing",
    "invalid_api_key",
    "401",
    "403",
)
_PROVIDER_AUTH_ERROR_MARKERS = (
    "invalid_api_key",
    "401",
    "403",
)


def _bounded_int_from_env(name: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, "") or "").strip() or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _llm_max_concurrent_requests() -> int:
    return _bounded_int_from_env(
        "AGVM_LLM_MAX_CONCURRENT_REQUESTS",
        default=3,
        minimum=1,
        maximum=8,
    )


def llm_provider_concurrency_limit() -> int:
    """Return the provider capacity used by the process-wide request semaphore."""

    return _llm_max_concurrent_requests()


def _llm_queue_timeout_seconds(request_timeout: float) -> float:
    local_budget = max(0.25, float(request_timeout or 3.0))
    configured = os.getenv("AGVM_LLM_QUEUE_TIMEOUT_SECONDS")
    if configured is not None and str(configured).strip():
        try:
            # The env value is a ceiling for abnormal local stalls, not
            # permission for first-payload calls to wait behind unrelated LLM
            # work longer than their own product budget.
            return max(0.05, min(float(configured), local_budget))
        except ValueError:
            pass
    return local_budget


def _llm_provider_semaphore() -> threading.BoundedSemaphore:
    global _LLM_PROVIDER_SEMAPHORE, _LLM_PROVIDER_SEMAPHORE_LIMIT
    limit = _llm_max_concurrent_requests()
    with _LLM_PROVIDER_LOCK:
        if _LLM_PROVIDER_SEMAPHORE is None or _LLM_PROVIDER_SEMAPHORE_LIMIT != limit:
            _LLM_PROVIDER_SEMAPHORE = threading.BoundedSemaphore(limit)
            _LLM_PROVIDER_SEMAPHORE_LIMIT = limit
        return _LLM_PROVIDER_SEMAPHORE


def _ensure_openai_strict_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        normalized = {key: _ensure_openai_strict_schema(value) for key, value in schema.items()}
        schema_type = normalized.get("type")
        if schema_type == "object":
            properties = dict(normalized.get("properties") or {})
            normalized["properties"] = properties
            normalized["additionalProperties"] = False
            normalized["required"] = list(properties.keys())
        if schema_type == "array" and "items" in normalized:
            normalized["items"] = _ensure_openai_strict_schema(normalized["items"])
        return normalized
    if isinstance(schema, list):
        return [_ensure_openai_strict_schema(item) for item in schema]
    return schema


def llm_enabled() -> bool:
    flag = os.getenv("AGVM_LLM_ENABLED", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    try:
        from hosted_credential_context import openai_provider_configured

        return bool(openai_provider_configured())
    except ImportError:
        return bool(str(os.getenv("OPENAI_API_KEY") or "").strip())


def _resolved_provider_api_key() -> str:
    """Resolve a request-scoped hosted credential before the local env key."""

    try:
        from hosted_credential_context import resolved_openai_api_key

        return str(resolved_openai_api_key() or "").strip()
    except ImportError:
        return str(os.getenv("OPENAI_API_KEY") or "").strip()


def llm_model() -> str:
    return os.getenv("AGVM_LLM_MODEL", DEFAULT_OPENAI_MODEL)


def compiler_model() -> str:
    return os.getenv("AGVM_COMPILER_MODEL", DEFAULT_COMPILER_MODEL)


def retrieval_model() -> str:
    return os.getenv("AGVM_RETRIEVAL_MODEL", DEFAULT_RETRIEVAL_MODEL)


def answer_model() -> str:
    return os.getenv("AGVM_ANSWER_MODEL", DEFAULT_ANSWER_MODEL)


def sleep_model() -> str:
    return os.getenv("AGVM_SLEEP_MODEL", DEFAULT_SLEEP_MODEL)


def _model_env_or(env_name: str, fallback: str) -> str:
    return str(os.getenv(env_name) or "").strip() or fallback


def planner_model() -> str:
    return _model_env_or("AGVM_PLANNER_MODEL", retrieval_model() or DEFAULT_PLANNER_MODEL)


def ai_spatial_model() -> str:
    return _model_env_or("AGVM_AI_SPATIAL_MODEL", retrieval_model() or DEFAULT_AI_SPATIAL_MODEL)


def branch_controller_model() -> str:
    return _model_env_or("AGVM_BRANCH_CONTROLLER_MODEL", retrieval_model() or DEFAULT_BRANCH_CONTROLLER_MODEL)


def evidence_judge_model() -> str:
    return _model_env_or("AGVM_EVIDENCE_JUDGE_MODEL", retrieval_model() or DEFAULT_EVIDENCE_JUDGE_MODEL)


def master_model() -> str:
    return _model_env_or("AGVM_MASTER_MODEL", answer_model() or DEFAULT_MASTER_MODEL)


def grow_semantic_model() -> str:
    return _model_env_or("AGVM_GROW_SEMANTIC_MODEL", retrieval_model() or DEFAULT_GROW_SEMANTIC_MODEL)


def clone_app_arbiter_model() -> str:
    return _model_env_or("AGVM_CLONE_APP_ARBITER_MODEL", retrieval_model())


def clone_app_sufficiency_model() -> str:
    return _model_env_or("AGVM_CLONE_APP_SUFFICIENCY_MODEL", answer_model())


def clone_app_speaker_model() -> str:
    return _model_env_or("AGVM_CLONE_APP_SPEAKER_MODEL", answer_model())


def clone_app_prefetch_model() -> str:
    return _model_env_or("AGVM_CLONE_APP_PREFETCH_MODEL", retrieval_model())


def clone_app_teach_model() -> str:
    return _model_env_or("AGVM_CLONE_APP_TEACH_MODEL", compiler_model())


def record_llm_result(
    role: str,
    *,
    path: str,
    error: str | None = None,
    model: str | None = None,
    timeout_seconds: float | None = None,
) -> None:
    if role not in _ROLE_NAMES:
        return
    _LLM_RUNTIME["requests"][role] += 1
    if path == "llm":
        _LLM_RUNTIME["success"][role] += 1
    else:
        _LLM_RUNTIME["fallback"][role] += 1
    _LLM_RUNTIME["last_path"][role] = path
    _LLM_RUNTIME["last_error"][role] = error
    if model:
        _LLM_RUNTIME["last_model"][role] = str(model)
    if timeout_seconds is not None:
        _LLM_RUNTIME["last_timeout_seconds"][role] = float(timeout_seconds)


def clear_llm_provider_auth_errors() -> int:
    """Clear stale provider auth failures after an explicit provider probe succeeds.

    A successful `/setup/provider/test` proves the currently configured key can
    reach the configured provider models.  That is enough to clear stale key or
    auth failures after a key rotation.  It is not enough to clear quota/billing
    failures, because provider model access can remain available when Responses
    calls are rejected for missing credits.
    """

    cleared = 0
    for role in _ROLE_NAMES:
        error = str(_LLM_RUNTIME["last_error"][role] or "").strip()
        if not error:
            continue
        lowered = error.lower()
        if any(marker in lowered for marker in _PROVIDER_AUTH_ERROR_MARKERS):
            _LLM_RUNTIME["last_error"][role] = None
            cleared += 1
    return cleared


def llm_runtime_status() -> dict[str, Any]:
    per_role: dict[str, Any] = {}
    for role in _ROLE_NAMES:
        requests = int(_LLM_RUNTIME["requests"][role])
        fallbacks = int(_LLM_RUNTIME["fallback"][role])
        per_role[role] = {
            "requests": requests,
            "success": int(_LLM_RUNTIME["success"][role]),
            "fallback": fallbacks,
            "last_path": _LLM_RUNTIME["last_path"][role],
            "last_error": _LLM_RUNTIME["last_error"][role],
            "last_queue_wait_ms": float(_LLM_RUNTIME["last_queue_wait_ms"][role] or 0.0),
            "last_model": _LLM_RUNTIME["last_model"][role],
            "last_timeout_seconds": float(_LLM_RUNTIME["last_timeout_seconds"][role] or 0.0),
            "queued": int(_LLM_RUNTIME["queued"][role]),
            "fallback_ratio": round(fallbacks / requests, 4) if requests else 0.0,
        }
    return per_role


def provider_auth_ok() -> bool | None:
    if not llm_enabled():
        return None
    known_errors = [str(_LLM_RUNTIME["last_error"][role] or "") for role in _ROLE_NAMES]
    if any("401" in error or "invalid_api_key" in error for error in known_errors):
        return False
    if any(int(_LLM_RUNTIME["success"][role]) > 0 for role in _ROLE_NAMES):
        return True
    return None


def _extract_text(payload: dict[str, Any]) -> str | None:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    collected: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            content_type = content.get("type")
            if content_type in {"output_text", "text"} and isinstance(content.get("text"), str):
                collected.append(content["text"])
    text = "".join(collected).strip()
    return text or None


def _responses_incomplete_reason(payload: dict[str, Any]) -> str | None:
    if str(payload.get("status") or "").strip().lower() != "incomplete":
        return None
    details = payload.get("incomplete_details")
    if isinstance(details, dict):
        reason = str(details.get("reason") or "").strip().lower()
        if reason:
            return reason
    return "unspecified"


def _nonnegative_usage_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def structured_json(
    *,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    model: str | None = None,
    timeout: float = 45.0,
    role: str = "compiler",
    max_output_tokens: int | None = None,
    api_key_override: str | None = None,
    execution_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    call_started_at = time.monotonic()
    resolved_model = model or llm_model()
    resolved_api_key = (
        str(api_key_override or "").strip()
        if api_key_override is not None
        else _resolved_provider_api_key()
    )
    provider_disabled = os.getenv("AGVM_LLM_ENABLED", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }
    if provider_disabled or not resolved_api_key:
        record_llm_result(role, path="fallback", error="llm_disabled", model=resolved_model, timeout_seconds=timeout)
        return None, "llm_disabled"

    request_body = {
        "model": resolved_model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": _ensure_openai_strict_schema(schema),
            }
        },
    }
    if max_output_tokens is not None:
        request_body["max_output_tokens"] = max(16, int(max_output_tokens))

    endpoint = str(
        os.getenv("AGVM_OPENAI_RESPONSES_URL")
        or os.getenv("OPENAI_RESPONSES_URL")
        or "https://api.openai.com/v1/responses"
    ).strip()
    request_bytes = json.dumps(request_body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url=endpoint,
        method="POST",
        data=request_bytes,
        headers={
            "Authorization": f"Bearer {resolved_api_key}",
            "Content-Type": "application/json",
        },
    )

    semaphore = _llm_provider_semaphore()
    queue_started_at = time.perf_counter()
    acquired = semaphore.acquire(timeout=_llm_queue_timeout_seconds(timeout))
    queue_wait_ms = round((time.perf_counter() - queue_started_at) * 1000.0, 2)
    if role in _ROLE_NAMES:
        _LLM_RUNTIME["last_queue_wait_ms"][role] = queue_wait_ms
        if queue_wait_ms > 1.0:
            _LLM_RUNTIME["queued"][role] += 1
    if not acquired:
        error = f"llm_queue_timeout:waited_ms={queue_wait_ms}"
        record_llm_result(role, path="fallback", error=error, model=resolved_model, timeout_seconds=timeout)
        return None, error

    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                response_bytes = response.read()
                payload = json.loads(response_bytes.decode("utf-8"))
        finally:
            semaphore.release()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        error = f"http_error:{exc.code}:{detail[:240]}"
        record_llm_result(role, path="fallback", error=error, model=resolved_model, timeout_seconds=timeout)
        return None, error
    except Exception as exc:  # noqa: BLE001
        error = f"transport_error:{exc}"
        record_llm_result(role, path="fallback", error=error, model=resolved_model, timeout_seconds=timeout)
        return None, error

    incomplete_reason = _responses_incomplete_reason(payload)
    if incomplete_reason:
        # Responses may include a truncated `output_text` alongside
        # status=incomplete. Never attempt to parse or attest that fragment:
        # it is not a conforming structured response even when its prefix looks
        # like JSON. The one permitted repair repeats the same AI-bound request
        # and schema with enough output budget to finish it.
        if incomplete_reason == "max_output_tokens" and max_output_tokens is not None and int(max_output_tokens) < 12000:
            retry_timeout = float(timeout) - (time.monotonic() - call_started_at)
            if retry_timeout <= 0.05:
                error = "incomplete_response:max_output_tokens:deadline_exhausted"
                record_llm_result(role, path="fallback", error=error, model=resolved_model, timeout_seconds=timeout)
                return None, error
            initial_usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            retry_execution: dict[str, Any] = {}
            parsed, retry_error = structured_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_name=schema_name,
                schema=schema,
                model=resolved_model,
                timeout=retry_timeout,
                role=role,
                max_output_tokens=12000,
                api_key_override=resolved_api_key,
                execution_metadata=retry_execution,
            )
            if execution_metadata is not None:
                execution_metadata.clear()
                execution_metadata.update(retry_execution)
                execution_metadata["provider_retry"] = {
                    "count": 1,
                    "reason": "max_output_tokens",
                    "initial_max_output_tokens": int(max_output_tokens),
                    "retry_max_output_tokens": 12000,
                    "initial_usage": {
                        key: _nonnegative_usage_count(initial_usage.get(key))
                        for key in ("input_tokens", "output_tokens", "total_tokens")
                    },
                }
            return parsed, retry_error
        error = f"incomplete_response:{incomplete_reason}"
        record_llm_result(role, path="fallback", error=error, model=resolved_model, timeout_seconds=timeout)
        return None, error

    text = _extract_text(payload)
    if not text:
        record_llm_result(role, path="fallback", error="missing_output_text", model=resolved_model, timeout_seconds=timeout)
        return None, "missing_output_text"

    try:
        parsed = json.loads(text)
        if execution_metadata is not None:
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            output_details = (
                usage.get("output_tokens_details")
                if isinstance(usage.get("output_tokens_details"), dict)
                else {}
            )
            def usage_count(value: Any) -> int:
                try:
                    return max(0, int(value or 0))
                except (TypeError, ValueError):
                    return 0

            execution_metadata.clear()
            execution_metadata.update(
                {
                    "schema_version": "agvm.ai_execution_attestation.v2",
                    "status": "completed",
                    "provider_executed": True,
                    "provider": "openai_compatible",
                    "endpoint_origin": endpoint.split("/v1/", 1)[0],
                    "response_id": str(payload.get("id") or payload.get("response_id") or "")[:256],
                    "model": str(payload.get("model") or resolved_model)[:256],
                    "role": str(role or "")[:80],
                    "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                    "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "usage": {
                        "input_tokens": usage_count(usage.get("input_tokens")),
                        "output_tokens": usage_count(usage.get("output_tokens")),
                        "reasoning_tokens": usage_count(
                            output_details.get("reasoning_tokens")
                            or usage.get("reasoning_tokens")
                        ),
                        "total_tokens": usage_count(usage.get("total_tokens")),
                    },
                }
            )
        record_llm_result(role, path="llm", error=None, model=resolved_model, timeout_seconds=timeout)
        return parsed, None
    except json.JSONDecodeError:
        record_llm_result(role, path="fallback", error="invalid_json", model=resolved_model, timeout_seconds=timeout)
        return None, "invalid_json"
