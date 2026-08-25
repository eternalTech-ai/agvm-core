# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import importlib
import io
import os
import socket
import sys
import urllib.error
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


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


def test_provider_key_test_is_bounded_non_persisting_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup_env = _setup_env_module()
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    candidate = "fixture-provider-key-non-persisting-1234567890"
    observed: dict[str, object] = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def _urlopen(request, *, timeout):
        observed["authorization"] = request.get_header("Authorization")
        observed["timeout"] = timeout
        observed["url"] = request.full_url
        return _Response()

    monkeypatch.setattr(setup_env.urllib.request, "urlopen", _urlopen)
    response = _setup_provider_client().post("/setup/provider/test", json={"api_key": candidate})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status"] == "valid"
    assert response.json()["capability"] == "model_access"
    assert response.json()["persisted"] is False
    assert observed["authorization"] == f"Bearer {candidate}"
    assert float(observed["timeout"]) <= setup_env.PROVIDER_KEY_TEST_TIMEOUT_MAX_SECONDS
    assert str(observed["url"]).startswith("https://api.openai.com/v1/models/")
    assert candidate not in response.text
    assert candidate not in caplog.text
    assert os.getenv("OPENAI_API_KEY") is None
    assert not (tmp_path / setup_env.MANAGED_ENV_FILENAME).exists()


def test_provider_key_test_returns_structured_rejection_without_upstream_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup_env = _setup_env_module()
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))
    candidate = "fixture-provider-key-rejected-0987654321"

    def _urlopen(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(f'{{"error":"rejected {candidate}"}}'.encode()),
        )

    monkeypatch.setattr(setup_env.urllib.request, "urlopen", _urlopen)
    response = _setup_provider_client().post("/setup/provider/test", json={"api_key": candidate})
    payload = response.json()["detail"]

    assert response.status_code == 401
    assert payload["ok"] is False
    assert payload["status"] == "rejected"
    assert payload["persisted"] is False
    assert payload["error"] == {
        "code": "provider_key_rejected",
        "message": "The provider rejected this key. Check the key and organization access.",
        "retryable": False,
    }
    assert candidate not in response.text
    assert not (tmp_path / setup_env.MANAGED_ENV_FILENAME).exists()


def test_provider_key_test_validation_never_reflects_candidate() -> None:
    candidate = "fixture-provider-key-reflection-guard-1234567890"
    client = _setup_provider_client()

    malformed = client.post(
        "/setup/provider/test",
        content=f'{{"api_key":"{candidate}"',
        headers={"Content-Type": "application/json"},
    )
    wrong_shape = client.post("/setup/provider/test", json={"api_key": {"secret": candidate}})

    assert malformed.status_code == 400
    assert wrong_shape.status_code == 400
    assert malformed.json()["detail"]["error"]["code"] == "invalid_json"
    assert wrong_shape.json()["detail"]["error"]["code"] == "provider_key_required"
    assert candidate not in malformed.text
    assert candidate not in wrong_shape.text


def test_provider_key_test_timeout_is_structured_and_non_persisting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup_env = _setup_env_module()
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))

    def _urlopen(_request, *, timeout):
        raise socket.timeout()

    monkeypatch.setattr(setup_env.urllib.request, "urlopen", _urlopen)
    response = _setup_provider_client().post(
        "/setup/provider/test",
        json={"api_key": "fixture-provider-key-timeout-1234567890"},
    )

    assert response.status_code == 504
    assert response.json()["detail"]["status"] == "timeout"
    assert response.json()["detail"]["error"]["code"] == "provider_timeout"
    assert response.json()["detail"]["persisted"] is False
    assert not (tmp_path / setup_env.MANAGED_ENV_FILENAME).exists()


def _setup_provider_client() -> TestClient:
    if "core_runtime_router" in sys.modules:
        router_module = importlib.reload(sys.modules["core_runtime_router"])
    else:
        router_module = importlib.import_module("core_runtime_router")

    app = FastAPI()
    app.include_router(router_module.create_core_runtime_router(app_name="AGVM test", app_version="test"))
    return TestClient(app)
