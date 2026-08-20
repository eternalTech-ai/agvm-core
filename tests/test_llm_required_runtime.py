from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))


def _main_module():
    if "main" in sys.modules:
        return importlib.reload(sys.modules["main"])
    return importlib.import_module("main")


def test_runtime_rejects_missing_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _main_module()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AGVM_LLM_ENABLED", raising=False)
    monkeypatch.delenv("AGVM_ALLOW_SETUP_WITHOUT_PROVIDER", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        main._validate_runtime_configuration()


def test_runtime_allows_setup_mode_without_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _main_module()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AGVM_LLM_ENABLED", raising=False)
    monkeypatch.setenv("AGVM_ALLOW_SETUP_WITHOUT_PROVIDER", "true")

    main._validate_runtime_configuration()


def test_runtime_rejects_explicit_llm_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _main_module()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AGVM_LLM_ENABLED", "false")
    monkeypatch.setenv("AGVM_ALLOW_SETUP_WITHOUT_PROVIDER", "true")

    with pytest.raises(RuntimeError, match="cannot start with AGVM_LLM_ENABLED=false"):
        main._validate_runtime_configuration()


def test_runtime_accepts_ai_driven_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _main_module()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AGVM_LLM_ENABLED", "true")

    main._validate_runtime_configuration()
