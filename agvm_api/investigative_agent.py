# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

try:
    from .ai_modules_v2 import (
        AiModuleContractError,
        canonical_sha256,
        validate_ai_execution_attestation,
    )
except ImportError:  # pragma: no cover - direct API runtime
    from ai_modules_v2 import (
        AiModuleContractError,
        canonical_sha256,
        validate_ai_execution_attestation,
    )


AI_ATTESTATION_SCHEMA_VERSION = "agvm.ai_execution_attestation.v2"
INVESTIGATIVE_AGENT_RESULT_SCHEMA_VERSION = "agvm.investigative_agent_result.v1"

_FORBIDDEN_REASONING_KEYS = {"analysis", "chain_of_thought", "reasoning", "thoughts"}
_NON_PROVIDER_MARKERS = {
    "deterministic",
    "fake",
    "fallback",
    "fixture",
    "heuristic",
    "mock",
    "none",
    "stub",
    "test",
}
_USAGE_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


Provider = Callable[[dict[str, Any]], Any]
TurnValidator = Callable[[dict[str, Any], int], tuple[dict[str, Any] | None, str | None]]
ToolBatchExecutor = Callable[[list[dict[str, Any]], int], list[dict[str, Any]]]
FinalValidator = Callable[[dict[str, Any]], str | None]
ContextBuilder = Callable[[int, str, dict[str, Any] | None, list[dict[str, Any]]], dict[str, Any]]


@dataclass(frozen=True)
class InvestigativeAgentBudget:
    """Domain-neutral bounds for a provider-led tool loop."""

    max_turns: int = 3
    max_tool_calls: int = 8
    max_repairs: int = 1
    max_no_progress_turns: int = 2
    provider_timeout_seconds: int = 60
    notebook_observation_limit: int = 24

    def as_dict(self) -> dict[str, int]:
        return {
            "max_turns": max(1, int(self.max_turns)),
            "max_tool_calls": max(0, int(self.max_tool_calls)),
            "max_repairs": max(0, int(self.max_repairs)),
            "max_no_progress_turns": max(1, int(self.max_no_progress_turns)),
            "provider_timeout_seconds": max(1, int(self.provider_timeout_seconds)),
            "notebook_observation_limit": max(1, int(self.notebook_observation_limit)),
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def contains_forbidden_reasoning(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().casefold() in _FORBIDDEN_REASONING_KEYS:
                return True
            if contains_forbidden_reasoning(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_forbidden_reasoning(item) for item in value)
    return False


def normalize_provider_result(value: Any) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    if isinstance(value, tuple):
        payload = value[0] if len(value) > 0 else None
        error = value[1] if len(value) > 1 else None
        metadata = value[2] if len(value) > 2 else {}
        return (
            dict(payload) if isinstance(payload, Mapping) else None,
            str(error) if error else None,
            dict(metadata) if isinstance(metadata, Mapping) else {},
        )
    if isinstance(value, Mapping):
        return dict(value), None, {}
    return None, "invalid_provider_result", {}


def _contains_non_provider_identity(value: Any) -> bool:
    markers = {
        marker
        for marker in re.split(r"[^a-z0-9]+", str(value or "").casefold())
        if marker
    }
    return bool(markers & _NON_PROVIDER_MARKERS)


def validate_provider_call_attestation(
    metadata: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate real provider execution without estimating usage."""

    request_sha256 = canonical_sha256(dict(request))
    output_sha256 = canonical_sha256(dict(payload))
    nested = metadata.get("ai_execution_attestation")
    if isinstance(nested, Mapping):
        attestation = validate_ai_execution_attestation(
            dict(nested),
            expected_request_sha256=request_sha256,
            expected_output_sha256=output_sha256,
        )
    else:
        attestation = validate_ai_execution_attestation(
            {
                "schema_version": AI_ATTESTATION_SCHEMA_VERSION,
                "status": "completed",
                "provider_executed": metadata.get("provider_executed"),
                "provider": metadata.get("provider"),
                "model": metadata.get("model"),
                "request_sha256": request_sha256,
                "output_sha256": output_sha256,
                "usage": metadata.get("usage"),
            },
            expected_request_sha256=request_sha256,
            expected_output_sha256=output_sha256,
        )
    if _contains_non_provider_identity(attestation.get("provider")) or _contains_non_provider_identity(
        attestation.get("model")
    ):
        raise AiModuleContractError("ai_execution_provider_invalid")
    usage = dict(attestation.get("usage") or {})
    if int(usage.get("total_tokens") or 0) <= 0:
        raise AiModuleContractError("ai_execution_usage_empty")
    if int(usage.get("total_tokens") or 0) < (
        int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    ):
        raise AiModuleContractError("ai_execution_usage_invalid")
    return dict(attestation)


def execution_ledger_entry(
    *,
    role: str,
    call_id: str,
    attestation: Mapping[str, Any],
    wave: int | None = None,
    brain_revision: str | None = None,
    parent_operation_id: str | None = None,
    child_call_id: str | None = None,
    billing_scope: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    entry = {
        "schema_version": "agvm.ai_execution_ledger_entry.v1",
        "role": str(role or "investigator"),
        "call_id": str(call_id or ""),
        "wave": wave,
        "brain_revision": str(brain_revision or "") or None,
        "parent_operation_id": str(parent_operation_id or "") or None,
        "child_call_id": str(child_call_id or "") or None,
        "billing_scope": str(billing_scope or "") or None,
        "idempotency_key": str(idempotency_key or "") or None,
        "attestation": dict(attestation),
    }
    entry["entry_sha256"] = stable_digest(entry)
    return entry


def aggregate_execution_ledger(
    entries: Sequence[Mapping[str, Any]],
    *,
    complete: bool,
    applicable: bool,
) -> dict[str, Any]:
    normalized = [dict(item) for item in entries if isinstance(item, Mapping)]
    provider_entries = [
        item
        for item in normalized
        if bool(dict(item.get("attestation") or {}).get("provider_executed"))
    ]
    usage = {
        field: sum(
            int(dict(dict(item.get("attestation") or {}).get("usage") or {}).get(field) or 0)
            for item in normalized
        )
        for field in _USAGE_FIELDS
    }
    providers = sorted(
        {
            str(dict(item.get("attestation") or {}).get("provider") or "").strip()
            for item in provider_entries
            if str(dict(item.get("attestation") or {}).get("provider") or "").strip()
        }
    )
    models = sorted(
        {
            str(dict(item.get("attestation") or {}).get("model") or "").strip()
            for item in provider_entries
            if str(dict(item.get("attestation") or {}).get("model") or "").strip()
        }
    )
    return {
        "schema_version": "agvm.ai_execution_ledger_aggregate.v1",
        "status": "completed" if complete else ("failed" if normalized else "not_executed"),
        "provider_executed": bool(provider_entries),
        "providers": providers,
        "models": models,
        "request_count": len(normalized),
        "successful_request_count": len(provider_entries),
        "usage": usage,
        "ledger_sha256": stable_digest(
            [str(item.get("entry_sha256") or stable_digest(item)) for item in normalized]
        ),
        "complete": bool(complete),
        "applicable": bool(applicable and complete),
    }


def _tool_signature(call: Mapping[str, Any]) -> str:
    return stable_digest(
        {
            "tool_name": str(call.get("tool_name") or ""),
            "arguments": dict(call.get("arguments") or {}),
        }
    )


def _failure_code(error: str | None) -> str:
    normalized = str(error or "provider_unavailable").casefold()
    if "timeout" in normalized:
        return "provider_timeout"
    if any(
        token in normalized
        for token in (
            "disabled",
            "missing",
            "unavailable",
            "api_key",
            "429",
            "quota",
            "insufficient_quota",
            "rate limit",
            "rate_limit",
            "credits",
        )
    ):
        return "provider_unavailable"
    return "provider_error"


def _call_provider_with_hard_timeout(
    provider: Provider,
    request: dict[str, Any],
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """Call a provider with an agent-owned wall-clock cap.

    Provider adapters receive ``timeout_seconds`` in the request, but that is a
    cooperative hint. Grow/Search investigations must still fail closed when an
    adapter or upstream client ignores it; otherwise the HTTP request can remain
    open until an outer server/proxy timeout and the UI has no recoverable state.
    """

    bounded_timeout = max(0.001, float(timeout_seconds))
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agvm-investigator-provider")
    future = executor.submit(provider, request)
    try:
        return normalize_provider_result(future.result(timeout=bounded_timeout))
    except FuturesTimeoutError:
        future.cancel()
        return None, f"provider_timeout_after_{bounded_timeout:.3f}s", {}
    except TimeoutError as exc:
        return None, str(exc), {}
    except Exception as exc:  # noqa: BLE001
        return None, str(exc), {}
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def run_investigative_agent(
    *,
    provider: Provider | None,
    schema_name: str,
    schema: Mapping[str, Any],
    role: str,
    system_prompt: str,
    context_builder: ContextBuilder,
    turn_validator: TurnValidator,
    tool_batch_executor: ToolBatchExecutor,
    final_validator: FinalValidator,
    allowed_tool_names: Sequence[str],
    budget: InvestigativeAgentBudget | None = None,
    initial_observations: Sequence[Mapping[str, Any]] | None = None,
    repair_limits_by_detail: Mapping[str, int] | None = None,
    deadline_monotonic: float | None = None,
    monotonic_clock: Callable[[], float] | None = None,
    deadline_stage_prefix: str | None = None,
) -> dict[str, Any]:
    """Run a bounded, attested, fail-closed provider/tool loop.

    Domain adapters own semantic validation and finalization. This function only
    enforces protocol shape, deduplication, provider attestations, repairs, and
    bounded progress.
    """

    resolved_budget = budget or InvestigativeAgentBudget()
    started_at = utc_now()
    observations = [dict(item) for item in list(initial_observations or []) if isinstance(item, Mapping)]
    activities: list[dict[str, Any]] = []
    execution_ledger: list[dict[str, Any]] = []
    tool_signatures: set[str] = set()
    provider_calls = 0
    base_turns = 0
    repairs_used = 0
    repair_counts: dict[str, int] = {}
    no_progress_turns = 0
    prior_progress_digest = stable_digest(observations)
    correction: dict[str, Any] | None = None
    last_payload: dict[str, Any] = {}
    allowed = {str(item) for item in allowed_tool_names}
    repair_limits = {
        str(key): max(0, int(value))
        for key, value in dict(repair_limits_by_detail or {}).items()
    }
    clock = monotonic_clock or time.monotonic
    deadline_prefix = str(deadline_stage_prefix or role or "investigator").strip()

    def deadline_detail(stage: str) -> str | None:
        if deadline_monotonic is None:
            return None
        remaining = float(deadline_monotonic) - float(clock())
        if remaining > 0.0:
            return None
        return (
            f"grow_wall_budget_exhausted:stage={deadline_prefix}_{stage};"
            f"remaining_seconds={remaining:.6f}"
        )

    def provider_timeout(stage: str) -> tuple[float | None, str | None]:
        detail = deadline_detail(stage)
        if detail is not None:
            return None, detail
        configured = max(0.001, float(resolved_budget.provider_timeout_seconds))
        if deadline_monotonic is None:
            return configured, None
        remaining = float(deadline_monotonic) - float(clock())
        if remaining <= 0.0:
            return None, deadline_detail(stage)
        return min(configured, remaining), None

    def repair_limit_for_detail(detail: str) -> int:
        if detail in repair_limits:
            return repair_limits[detail]
        prefix_matches = [
            (len(pattern), limit)
            for pattern, limit in repair_limits.items()
            if pattern.endswith("*") and detail.startswith(pattern[:-1])
        ]
        if prefix_matches:
            return max(prefix_matches)[1]
        return repair_limits.get("*", max(0, int(resolved_budget.max_repairs)))

    def begin_repair(detail: str) -> bool:
        nonlocal repairs_used
        normalized = str(detail or "invalid_provider_output")
        per_detail_limit = repair_limit_for_detail(normalized)
        if repairs_used >= resolved_budget.max_repairs:
            return False
        if repair_counts.get(normalized, 0) >= per_detail_limit:
            return False
        repairs_used += 1
        repair_counts[normalized] = repair_counts.get(normalized, 0) + 1
        return True

    def incomplete(code: str, detail: str | None) -> dict[str, Any]:
        aggregate = aggregate_execution_ledger(execution_ledger, complete=False, applicable=False)
        return {
            "schema_version": INVESTIGATIVE_AGENT_RESULT_SCHEMA_VERSION,
            "status": "incomplete",
            "complete": False,
            "applicable": False,
            "started_at": started_at,
            "completed_at": utc_now(),
            "budget": resolved_budget.as_dict(),
            "usage": {
                "provider_calls": provider_calls,
                "successful_provider_calls": len(execution_ledger),
                "tool_calls": len(activities),
                "repair_turns": repairs_used,
            },
            "activities": activities,
            "observations": observations,
            "payload": last_payload,
            "failure": {"code": str(code), "detail": str(detail or "")[:1000]},
            "ai_execution_ledger": execution_ledger,
            "ai_execution_attestation": aggregate,
        }

    if provider is None:
        return incomplete("provider_unavailable", f"{role}_provider_not_configured")

    max_provider_calls = max(1, resolved_budget.max_turns) + max(0, resolved_budget.max_repairs)
    while provider_calls < max_provider_calls:
        if correction is None:
            if base_turns >= max(1, resolved_budget.max_turns):
                break
            base_turns += 1
            phase = "investigation" if base_turns == 1 else "ai_review"
        else:
            phase = "schema_repair"
        turn_number = provider_calls + 1
        call_stage = "ai_review" if phase == "ai_review" else phase
        timeout_seconds, expired_detail = provider_timeout(call_stage)
        if expired_detail is not None or timeout_seconds is None:
            return incomplete("wall_budget_exhausted", expired_detail)
        notebook = context_builder(turn_number, phase, correction, observations)
        request = {
            "schema_name": str(schema_name),
            "schema": dict(schema),
            "system_prompt": str(system_prompt),
            "user_prompt": json.dumps(notebook, ensure_ascii=True, sort_keys=True),
            "timeout_seconds": timeout_seconds,
        }
        provider_calls += 1
        payload, provider_error, metadata = _call_provider_with_hard_timeout(
            provider,
            request,
            timeout_seconds=timeout_seconds,
        )

        expired_detail = deadline_detail(call_stage)
        if expired_detail is not None and (provider_error or payload is None):
            return incomplete("wall_budget_exhausted", expired_detail)
        if provider_error or payload is None:
            repair_detail = _failure_code(provider_error)
            if begin_repair(repair_detail):
                correction = {
                    "code": _failure_code(provider_error),
                    "detail": str(provider_error or "invalid_provider_result")[:500],
                    "instruction": "Retry once with the identical output schema; do not invent tool results.",
                }
                continue
            return incomplete(_failure_code(provider_error), provider_error)

        try:
            attestation = validate_provider_call_attestation(metadata, request=request, payload=payload)
        except AiModuleContractError as exc:
            if begin_repair("provider_attestation_invalid"):
                correction = {
                    "code": "provider_attestation_invalid",
                    "detail": exc.code,
                    "instruction": "Retry once with the identical output schema and a complete real-provider attestation.",
                }
                continue
            return incomplete("provider_attestation_invalid", exc.code)
        execution_ledger.append(
            execution_ledger_entry(
                role=role,
                call_id=f"{role}::{turn_number}",
                wave=turn_number,
                attestation=attestation,
            )
        )
        expired_detail = deadline_detail(call_stage)
        if expired_detail is not None:
            return incomplete("wall_budget_exhausted", expired_detail)

        if contains_forbidden_reasoning(payload):
            validation_error = "forbidden_reasoning_field"
            normalized_payload = None
        else:
            normalized_payload, validation_error = turn_validator(dict(payload), turn_number)
        if validation_error or normalized_payload is None:
            repair_detail = str(validation_error or "invalid_provider_output")
            if begin_repair(repair_detail):
                correction = {
                    "code": "invalid_provider_output",
                    "detail": str(validation_error or "invalid_provider_output")[:500],
                    "instruction": "Correct only the reported protocol error using the identical schema.",
                }
                continue
            return incomplete("invalid_provider_output", validation_error)
        correction = None
        last_payload = dict(normalized_payload)

        raw_tool_calls = normalized_payload.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            return incomplete("invalid_provider_output", "tool_calls_invalid")
        tool_calls: list[dict[str, Any]] = []
        for raw_call in raw_tool_calls:
            if not isinstance(raw_call, Mapping):
                return incomplete("invalid_provider_output", "invalid_tool_call")
            call = dict(raw_call)
            call_id = str(call.get("call_id") or "").strip()
            tool_name = str(call.get("tool_name") or "").strip()
            if not call_id or tool_name not in allowed or not isinstance(call.get("arguments"), Mapping):
                return incomplete("invalid_provider_output", "invalid_tool_call")
            signature = _tool_signature(call)
            if signature in tool_signatures:
                return incomplete("repeated_equivalent_query", f"{tool_name}:{call_id}")
            tool_signatures.add(signature)
            tool_calls.append(call)
        if len(activities) + len(tool_calls) > resolved_budget.max_tool_calls:
            return incomplete("investigation_budget_exhausted", "tool_call_budget_exhausted")

        if tool_calls:
            expired_detail = deadline_detail("tool_batch")
            if expired_detail is not None:
                return incomplete("wall_budget_exhausted", expired_detail)
            try:
                batch_results = tool_batch_executor(tool_calls, turn_number)
            except Exception as exc:  # noqa: BLE001
                return incomplete("investigation_tool_failed", str(exc))
            expired_detail = deadline_detail("tool_batch")
            if expired_detail is not None:
                return incomplete("wall_budget_exhausted", expired_detail)
            if not isinstance(batch_results, list) or len(batch_results) != len(tool_calls):
                return incomplete("investigation_tool_failed", "tool_batch_result_shape_invalid")
            for call, raw_result in zip(tool_calls, batch_results, strict=True):
                result = dict(raw_result) if isinstance(raw_result, Mapping) else {}
                if (
                    result.get("usable") is False
                    and result.get("reviewable") is not True
                ) or result.get("error"):
                    return incomplete(
                        "investigation_tool_failed",
                        str(result.get("error") or f"{call['tool_name']}:unusable_result"),
                    )
                activity = {
                    "iteration": turn_number,
                    "call_id": str(call.get("call_id") or ""),
                    "tool_name": str(call.get("tool_name") or ""),
                    "arguments_sha256": stable_digest(dict(call.get("arguments") or {})),
                    "result_sha256": stable_digest(result),
                }
                activities.append(activity)
                observations.append({"call": call, "result": result})

        progress_digest = stable_digest(
            {
                "payload": normalized_payload,
                "latest_results": observations[-len(tool_calls) :] if tool_calls else [],
            }
        )
        if progress_digest == prior_progress_digest:
            no_progress_turns += 1
        else:
            no_progress_turns = 0
        prior_progress_digest = progress_digest
        if no_progress_turns >= max(1, resolved_budget.max_no_progress_turns):
            return incomplete("investigation_no_progress", "two_turns_without_evidence_or_decision_progress")

        status = str(normalized_payload.get("status") or "").strip().casefold()
        if status == "continue":
            if not tool_calls:
                if begin_repair("continue_response_requires_tool_calls"):
                    correction = {
                        "code": "invalid_provider_output",
                        "detail": "continue_response_requires_tool_calls",
                        "instruction": (
                            "The investigation cannot continue without a concrete next action. "
                            "Review the unchanged evidence notebook and return one schema-valid, "
                            "domain-authorized tool action or a terminal clarification/decision."
                        ),
                    }
                    continue
                return incomplete("invalid_provider_output", "continue_response_requires_tool_calls")
            continue
        if status not in {"needs_clarification", "complete"}:
            return incomplete("invalid_provider_output", "status_invalid")
        final_error = final_validator(normalized_payload)
        if final_error:
            if begin_repair(str(final_error)):
                correction = {
                    "code": "invalid_provider_output",
                    "detail": str(final_error)[:500],
                    "instruction": "Correct the final evidence-bound result using the identical schema.",
                }
                continue
            return incomplete("invalid_provider_output", final_error)

        complete = status == "complete"
        aggregate = aggregate_execution_ledger(
            execution_ledger,
            complete=complete,
            applicable=complete,
        )
        return {
            "schema_version": INVESTIGATIVE_AGENT_RESULT_SCHEMA_VERSION,
            "status": status,
            "complete": complete,
            "applicable": complete,
            "started_at": started_at,
            "completed_at": utc_now(),
            "budget": resolved_budget.as_dict(),
            "usage": {
                "provider_calls": provider_calls,
                "successful_provider_calls": len(execution_ledger),
                "tool_calls": len(activities),
                "repair_turns": repairs_used,
            },
            "activities": activities,
            "observations": observations,
            "payload": normalized_payload,
            "failure": None,
            "ai_execution_ledger": execution_ledger,
            "ai_execution_attestation": aggregate,
        }

    return incomplete("investigation_budget_exhausted", "turn_budget_exhausted")
