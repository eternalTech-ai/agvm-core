from __future__ import annotations

import os
import tempfile
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
    "OPENAI_API_KEY",
}


def managed_env_path() -> Path:
    data_dir = Path(os.getenv("AGVM_LAB_DATA_DIR") or (API_DIR / "data")).expanduser()
    return data_dir / MANAGED_ENV_FILENAME


def load_managed_env_into_process(*, override: bool = True) -> None:
    path = managed_env_path()
    if load_dotenv and path.exists():
        load_dotenv(path, override=override)


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
