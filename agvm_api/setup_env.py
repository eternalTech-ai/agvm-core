# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
import secrets
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:  # pragma: no cover - dependency is present in the packaged API image.
    dotenv_values = None
    load_dotenv = None


API_DIR = Path(__file__).resolve().parent
MANAGED_ENV_FILENAME = "agvm_runtime.env"
MANAGED_ENV_SCHEMA_VERSION = "agvm.setup_env.v1"
PROVIDER_KEY_TEST_SCHEMA_VERSION = "agvm.provider_key_test.v1"
PROVIDER_KEY_TEST_TIMEOUT_SECONDS = 5.0
PROVIDER_KEY_TEST_TIMEOUT_MAX_SECONDS = 8.0
GROW_PREVIEW_BINDING_SECRET_ENV = "AGVM_GROW_PREVIEW_BINDING_SECRET"
GROW_PREVIEW_BINDING_SECRET_MIN_BYTES = 32

MANAGED_ENV_KEYS = {
    "AGVM_DEFAULT_BRAIN_ID",
    "AGVM_LLM_ENABLED",
    "AGVM_LLM_MODEL",
    "AGVM_COMPILER_MODEL",
    "AGVM_RETRIEVAL_MODEL",
    "AGVM_ANSWER_MODEL",
    "AGVM_SLEEP_MODEL",
    "AGVM_CLONE_APP_ARBITER_MODEL",
    "AGVM_CLONE_APP_SUFFICIENCY_MODEL",
    "AGVM_CLONE_APP_SPEAKER_MODEL",
    "AGVM_CLONE_APP_PREFETCH_MODEL",
    "AGVM_CLONE_APP_TEACH_MODEL",
    GROW_PREVIEW_BINDING_SECRET_ENV,
    "OPENAI_API_KEY",
}


class ProviderKeyTestError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        message: str,
        retryable: bool,
        status: str,
        status_code: int,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status
        self.status_code = status_code

    def response_detail(self) -> dict[str, Any]:
        return _provider_key_test_payload(
            ok=False,
            status=self.status,
            error={
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
        )


def managed_env_path() -> Path:
    data_dir = Path(os.getenv("AGVM_LAB_DATA_DIR") or (API_DIR / "data")).expanduser()
    return data_dir / MANAGED_ENV_FILENAME


def load_managed_env_into_process(*, override: bool = True) -> None:
    path = managed_env_path()
    explicit_grow_secret = str(os.getenv(GROW_PREVIEW_BINDING_SECRET_ENV) or "").strip()
    if load_dotenv and path.exists():
        load_dotenv(path, override=override)
    if explicit_grow_secret:
        os.environ[GROW_PREVIEW_BINDING_SECRET_ENV] = explicit_grow_secret
    ensure_grow_preview_binding_secret()


def ensure_grow_preview_binding_secret() -> dict[str, Any]:
    configured = str(os.getenv(GROW_PREVIEW_BINDING_SECRET_ENV) or "").strip()
    if configured:
        _validate_grow_preview_binding_secret(configured)
        return {
            "configured": True,
            "generated": False,
            "source": "process_or_managed_env",
        }

    managed_secret = str(
        read_managed_env_values().get(GROW_PREVIEW_BINDING_SECRET_ENV) or ""
    ).strip()
    if managed_secret:
        _validate_grow_preview_binding_secret(managed_secret)
        os.environ[GROW_PREVIEW_BINDING_SECRET_ENV] = managed_secret
        return {
            "configured": True,
            "generated": False,
            "source": "managed_runtime_env",
        }

    generated = secrets.token_urlsafe(48)
    _validate_grow_preview_binding_secret(generated)
    save_managed_env_values({GROW_PREVIEW_BINDING_SECRET_ENV: generated})
    return {
        "configured": True,
        "generated": True,
        "source": "managed_runtime_env",
    }


def read_managed_env_values() -> dict[str, str]:
    path = managed_env_path()
    if not path.exists() or not dotenv_values:
        return {}
    return {
        str(key): str(value)
        for key, value in dict(dotenv_values(path)).items()
        if key in MANAGED_ENV_KEYS and value is not None
    }


def save_managed_env_values(updates: dict[str, str]) -> dict[str, Any]:
    filtered = {}
    for key, value in dict(updates or {}).items():
        env_key = str(key)
        env_value = str(value or "").strip()
        if env_key not in MANAGED_ENV_KEYS or not env_value:
            continue
        if "\n" in env_value or "\r" in env_value:
            raise ValueError(f"{env_key}_must_be_single_line")
        filtered[env_key] = env_value
    if not filtered:
        raise ValueError("no_supported_env_values")

    path = managed_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_managed_env_values()
    merged = {**current, **filtered}

    if path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        _chmod_private(backup_path)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{MANAGED_ENV_FILENAME}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("# AGVM managed runtime env. Written by the local setup UI.\n")
            handle.write("# Do not commit this file. It lives in the Docker/local data volume.\n")
            for key in sorted(merged):
                handle.write(f"{key}={_quote_env_value(merged[key])}\n")
        _chmod_private(tmp_path)
        os.replace(tmp_path, path)
        _chmod_private(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    for key, value in filtered.items():
        os.environ[key] = value

    return managed_env_status()


def managed_env_status() -> dict[str, Any]:
    managed_values = read_managed_env_values()
    provider_value = str(os.getenv("OPENAI_API_KEY") or "").strip()
    managed_provider_value = str(managed_values.get("OPENAI_API_KEY") or "").strip()
    effective_provider_value = managed_provider_value or provider_value
    if managed_provider_value:
        provider_source = "managed_runtime_env"
    elif provider_value:
        provider_source = "process_env"
    else:
        provider_source = "missing"
    return {
        "schema_version": MANAGED_ENV_SCHEMA_VERSION,
        "managed_env_path": str(managed_env_path()),
        "managed_keys": sorted(key for key, value in managed_values.items() if str(value or "").strip()),
        "provider": {
            "configured": bool(effective_provider_value),
            "masked": _mask_secret(effective_provider_value),
            "source": provider_source,
        },
        "llm": {
            "enabled": str(_env_or_managed(managed_values, "AGVM_LLM_ENABLED") or "true").strip().lower() not in {"0", "false", "no", "off"},
            "model": _env_or_managed(managed_values, "AGVM_LLM_MODEL"),
            "compiler_model": _env_or_managed(managed_values, "AGVM_COMPILER_MODEL"),
            "retrieval_model": _env_or_managed(managed_values, "AGVM_RETRIEVAL_MODEL"),
            "answer_model": _env_or_managed(managed_values, "AGVM_ANSWER_MODEL"),
            "sleep_model": _env_or_managed(managed_values, "AGVM_SLEEP_MODEL"),
            "clone_app": {
                "arbiter_model": _env_or_managed(managed_values, "AGVM_CLONE_APP_ARBITER_MODEL"),
                "sufficiency_model": _env_or_managed(managed_values, "AGVM_CLONE_APP_SUFFICIENCY_MODEL"),
                "speaker_model": _env_or_managed(managed_values, "AGVM_CLONE_APP_SPEAKER_MODEL"),
                "prefetch_model": _env_or_managed(managed_values, "AGVM_CLONE_APP_PREFETCH_MODEL"),
                "teach_model": _env_or_managed(managed_values, "AGVM_CLONE_APP_TEACH_MODEL"),
            },
        },
        "runtime": {
            "default_brain_id": _env_or_managed(managed_values, "AGVM_DEFAULT_BRAIN_ID"),
        },
    }


def test_openai_provider_key(
    api_key: str,
    *,
    timeout_seconds: float = PROVIDER_KEY_TEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    candidate = str(api_key or "").strip()
    if not candidate:
        raise ProviderKeyTestError(
            "provider_key_required",
            message="Enter a provider key before testing.",
            retryable=False,
            status="invalid_request",
            status_code=400,
        )
    if len(candidate) > 4096 or "\n" in candidate or "\r" in candidate:
        raise ProviderKeyTestError(
            "provider_key_invalid_format",
            message="The provider key must be a single value no longer than 4096 characters.",
            retryable=False,
            status="invalid_request",
            status_code=400,
        )

    model = str(os.getenv("AGVM_LLM_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    timeout = max(1.0, min(float(timeout_seconds), PROVIDER_KEY_TEST_TIMEOUT_MAX_SECONDS))
    request = urllib.request.Request(
        url=f"https://api.openai.com/v1/models/{urllib.parse.quote(model, safe='')}",
        method="GET",
        headers={"Authorization": f"Bearer {candidate}", "Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(getattr(response, "status", 200)) != 200:
                raise ProviderKeyTestError(
                    "provider_capability_unavailable",
                    message="The provider could not verify model access.",
                    retryable=True,
                    status="unavailable",
                    status_code=503,
                )
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404}:
            raise ProviderKeyTestError(
                "provider_key_rejected",
                message="The provider rejected this key. Check the key and organization access.",
                retryable=False,
                status="rejected",
                status_code=401,
            ) from None
        if exc.code == 429:
            raise ProviderKeyTestError(
                "provider_rate_limited",
                message="The provider is temporarily rate limited. Try the test again shortly.",
                retryable=True,
                status="unavailable",
                status_code=503,
            ) from None
        raise ProviderKeyTestError(
            "provider_unavailable",
            message="The provider could not be reached. The current saved key was not changed.",
            retryable=True,
            status="unavailable",
            status_code=503,
        ) from None
    except (TimeoutError, socket.timeout):
        raise ProviderKeyTestError(
            "provider_timeout",
            message="The provider capability check timed out. The current saved key was not changed.",
            retryable=True,
            status="timeout",
            status_code=504,
        ) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise ProviderKeyTestError(
                "provider_timeout",
                message="The provider capability check timed out. The current saved key was not changed.",
                retryable=True,
                status="timeout",
                status_code=504,
            ) from None
        raise ProviderKeyTestError(
            "provider_unavailable",
            message="The provider could not be reached. The current saved key was not changed.",
            retryable=True,
            status="unavailable",
            status_code=503,
        ) from None

    return _provider_key_test_payload(ok=True, status="valid", model=model)


def _provider_key_test_payload(
    *,
    ok: bool,
    status: str,
    model: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": PROVIDER_KEY_TEST_SCHEMA_VERSION,
        "ok": ok,
        "provider": "openai",
        "status": status,
        "capability": "model_access",
        "persisted": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if model:
        payload["model"] = model
    if error:
        payload["error"] = error
    return payload


def _env_or_managed(managed_values: dict[str, str], key: str) -> str:
    return str(managed_values.get(key) or os.getenv(key) or "").strip()


def _quote_env_value(value: str) -> str:
    text = str(value or "")
    if not text:
        return '""'
    if any(char.isspace() for char in text) or any(char in text for char in ('"', "'", "#", "\\")):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _validate_grow_preview_binding_secret(value: str) -> None:
    if len(str(value).encode("utf-8")) < GROW_PREVIEW_BINDING_SECRET_MIN_BYTES:
        raise ValueError("AGVM_GROW_PREVIEW_BINDING_SECRET_must_be_at_least_32_bytes")


def _mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 10:
        return "configured"
    return f"{text[:6]}...{text[-4:]}"


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
