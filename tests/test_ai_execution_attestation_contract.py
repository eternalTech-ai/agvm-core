# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib

import pytest

from agvm_api.ai_modules_v2 import (
    AiModuleContractError,
    validate_ai_execution_attestation,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _attestation(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "agvm.ai_execution_attestation.v2",
        "status": "completed",
        "provider_executed": True,
        "provider": "openai_compatible",
        "model": "gpt-5-mini",
        "request_sha256": _digest("request"),
        "output_sha256": _digest("output"),
        "usage": {
            "input_tokens": 21,
            "output_tokens": 8,
            "reasoning_tokens": 3,
            "total_tokens": 32,
        },
    }
    payload.update(overrides)
    return payload


def test_real_completed_provider_attestation_is_applicable_without_invented_signature() -> None:
    result = validate_ai_execution_attestation(
        _attestation(),
        expected_request_sha256=_digest("request"),
        expected_output_sha256=_digest("output"),
    )

    assert result["provider_executed"] is True
    assert result["applicable"] is True
    assert result["legacy_read_only"] is False
    assert "signature" not in result


@pytest.mark.parametrize(
    ("field", "identity"),
    [
        ("provider", "heuristic"),
        ("provider", "fallback-provider"),
        ("provider", "deterministic_runtime"),
        ("provider", "mock"),
        ("provider", "none"),
        ("provider", "fake-openai-compatible"),
        ("model", "heuristic-model"),
        ("model", "fallback"),
        ("model", "deterministic"),
        ("model", "mock-model"),
        ("model", "none"),
        ("model", "test-retrieval-model"),
    ],
)
def test_non_real_provider_or_model_never_attests_ai_execution(
    field: str,
    identity: str,
) -> None:
    with pytest.raises(AiModuleContractError) as caught:
        validate_ai_execution_attestation(_attestation(**{field: identity}))

    assert caught.value.code == f"ai_execution_{field}_invalid"


@pytest.mark.parametrize("provider_executed", [None, False, 0, 1, "true"])
def test_provider_execution_must_be_explicit_boolean_true(provider_executed: object) -> None:
    payload = _attestation()
    if provider_executed is None:
        payload.pop("provider_executed")
    else:
        payload["provider_executed"] = provider_executed

    with pytest.raises(AiModuleContractError) as caught:
        validate_ai_execution_attestation(payload)

    assert caught.value.code == "ai_execution_provider_not_executed"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"expected_request_sha256": _digest("different-request")},
            "ai_execution_request_sha256_mismatch",
        ),
        (
            {"expected_output_sha256": _digest("different-output")},
            "ai_execution_output_sha256_mismatch",
        ),
    ],
)
def test_expected_request_and_output_digests_must_match(
    kwargs: dict[str, str],
    code: str,
) -> None:
    with pytest.raises(AiModuleContractError) as caught:
        validate_ai_execution_attestation(_attestation(), **kwargs)

    assert caught.value.code == code


def test_duplicate_digest_aliases_cannot_disagree() -> None:
    with pytest.raises(AiModuleContractError) as caught:
        validate_ai_execution_attestation(
            _attestation(request_digest=_digest("different-request"))
        )

    assert caught.value.code == "ai_execution_request_sha256_mismatch"


def test_v1_is_readable_but_never_applicable() -> None:
    legacy = {
        "schema_version": "agvm.ai_execution_attestation.v1",
        "status": "completed",
        "provider": "openai_compatible",
        "model": "gpt-4.1-mini",
        "request_sha256": _digest("legacy-request"),
        "output_sha256": _digest("legacy-output"),
    }

    result = validate_ai_execution_attestation(legacy, allow_legacy_read=True)

    assert result["legacy_read_only"] is True
    assert result["provider_executed"] is False
    assert result["applicable"] is False
    assert "signature" not in result

    with pytest.raises(AiModuleContractError) as caught:
        validate_ai_execution_attestation(legacy)
    assert caught.value.code == "ai_execution_attestation_legacy_not_applicable"


def test_weak_v2_cannot_be_downgraded_to_legacy_read_mode() -> None:
    weak = _attestation()
    weak.pop("provider_executed")

    with pytest.raises(AiModuleContractError) as caught:
        validate_ai_execution_attestation(weak, allow_legacy_read=True)

    assert caught.value.code == "ai_execution_provider_not_executed"
