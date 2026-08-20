from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))


def _setup_env_module():
    if "setup_env" in sys.modules:
        return importlib.reload(sys.modules["setup_env"])
    return importlib.import_module("setup_env")


def test_managed_env_save_persists_allowed_keys_and_updates_process_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup_env = _setup_env_module()
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider_key = "fixture-provider-value-7890"
    provider_env = "OPENAI_API_KEY"

    status = setup_env.save_managed_env_values(
        {
            provider_env: provider_key,
            "AGVM_DEFAULT_BRAIN_ID": "local_brain",
            "AGVM_LLM_ENABLED": "true",
            "AGVM_CLONE_APP_SPEAKER_MODEL": "clone-speaker-model",
            "AGVM_CLONE_APP_TEACH_MODEL": "clone-teach-model",
            "UNRELATED": "ignored",
        }
    )

    managed_file = tmp_path / "agvm_runtime.env"
    text = managed_file.read_text(encoding="utf-8")
    assert managed_file.exists()
    assert f"{provider_env}={provider_key}" in text
    assert "AGVM_DEFAULT_BRAIN_ID=local_brain" in text
    assert "AGVM_CLONE_APP_SPEAKER_MODEL=clone-speaker-model" in text
    assert "AGVM_CLONE_APP_TEACH_MODEL=clone-teach-model" in text
    assert "UNRELATED" not in text
    assert os.environ["OPENAI_API_KEY"] == provider_key
    assert status["provider"]["configured"] is True
    assert status["provider"]["source"] == "managed_runtime_env"
    assert status["provider"]["masked"].endswith("7890")
    assert status["runtime"]["default_brain_id"] == "local_brain"
    assert status["llm"]["clone_app"]["speaker_model"] == "clone-speaker-model"
    assert status["llm"]["clone_app"]["teach_model"] == "clone-teach-model"


def test_managed_env_save_creates_backup_without_overwriting_host_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup_env = _setup_env_module()
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))

    setup_env.save_managed_env_values({"OPENAI_API_KEY": "fixture-first-value"})
    setup_env.save_managed_env_values({"OPENAI_API_KEY": "fixture-second-value"})

    managed_file = tmp_path / "agvm_runtime.env"
    backup_file = tmp_path / "agvm_runtime.env.bak"
    assert "fixture-second-value" in managed_file.read_text(encoding="utf-8")
    assert "fixture-first-value" in backup_file.read_text(encoding="utf-8")


def test_managed_env_status_uses_file_when_process_env_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup_env = _setup_env_module()
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider_env = "OPENAI_API_KEY"
    provider_key = "fixture-file-only-1234"

    managed_file = tmp_path / "agvm_runtime.env"
    managed_file.write_text(
        f"{provider_env}={provider_key}\n"
        "AGVM_DEFAULT_BRAIN_ID=file_brain\n"
        "AGVM_LLM_MODEL=gpt-test\n"
        "AGVM_CLONE_APP_ARBITER_MODEL=clone-arbiter-file\n",
        encoding="utf-8",
    )

    status = setup_env.managed_env_status()

    assert status["provider"]["configured"] is True
    assert status["provider"]["source"] == "managed_runtime_env"
    assert status["provider"]["masked"].endswith("1234")
    assert status["runtime"]["default_brain_id"] == "file_brain"
    assert status["llm"]["model"] == "gpt-test"
    assert status["llm"]["clone_app"]["arbiter_model"] == "clone-arbiter-file"


def test_managed_env_rejects_multiline_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup_env = _setup_env_module()
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="OPENAI_API_KEY_must_be_single_line"):
        setup_env.save_managed_env_values({"OPENAI_API_KEY": "fixture-line-1\nfixture-line-2"})


def test_setup_env_router_accepts_clone_app_model_policy() -> None:
    from core_runtime_router import SetupEnvSaveRequest, _setup_env_updates_from_payload

    payload = SetupEnvSaveRequest(
        agvm_clone_app_arbiter_model="clone-arbiter-model",
        agvm_clone_app_sufficiency_model="clone-sufficiency-model",
        agvm_clone_app_speaker_model="clone-speaker-model",
        agvm_clone_app_prefetch_model="clone-prefetch-model",
        agvm_clone_app_teach_model="clone-teach-model",
    )

    updates = _setup_env_updates_from_payload(payload)

    assert updates["AGVM_CLONE_APP_ARBITER_MODEL"] == "clone-arbiter-model"
    assert updates["AGVM_CLONE_APP_SUFFICIENCY_MODEL"] == "clone-sufficiency-model"
    assert updates["AGVM_CLONE_APP_SPEAKER_MODEL"] == "clone-speaker-model"
    assert updates["AGVM_CLONE_APP_PREFETCH_MODEL"] == "clone-prefetch-model"
    assert updates["AGVM_CLONE_APP_TEACH_MODEL"] == "clone-teach-model"
