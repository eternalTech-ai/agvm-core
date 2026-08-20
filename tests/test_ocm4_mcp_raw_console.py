from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "agvm_cockpit_prototype" / "src" / "new-ui"


def test_mcp_raw_console_is_a_public_core_mode() -> None:
    mode_rail = _read(UI_DIR / "shell" / "ModeRail.tsx")
    ts_classification = _read(UI_DIR / "modules" / "coreModeClassification.ts")
    py_classification = _read(ROOT / "agvm_api" / "route_classification.py")

    assert '"mcp_raw_console"' in mode_rail
    assert '{ key: "mcp_raw_console", label: "Raw MCP"' in mode_rail
    assert 'mode: "mcp_raw_console"' in ts_classification
    assert "agvm_core_mcp_raw_console" in ts_classification
    assert '"mcp_raw_console": SurfaceClassification("core", "agvm_core_mcp_raw_console", True' in py_classification


def test_mcp_raw_console_is_rendered_in_core_and_monolith_stages() -> None:
    core_stage = _read(UI_DIR / "shell" / "CoreModeWorkspaceStage.tsx")
    monolith_stage = _read(UI_DIR / "shell" / "ModeWorkspaceStage.tsx")
    neural_app = _read(UI_DIR / "NeuralCockpitApp.tsx")
    proof_dock = _read(UI_DIR / "shell" / "ProofDock.tsx")

    assert "McpRawConsoleWorkspace" in core_stage
    assert 'props.mode === "mcp_raw_console"' in core_stage
    assert "McpRawConsoleWorkspace" in monolith_stage
    assert 'props.mode === "mcp_raw_console"' in monolith_stage
    assert '"mcp_raw_console"' in neural_app
    assert 'mode === "mcp_raw_console"' in neural_app
    assert "MCP Raw Console" in proof_dock
    assert "Raw MCP tool console" in proof_dock


def test_mcp_raw_console_uses_contract_registry_and_guarded_execution() -> None:
    client = _read(UI_DIR / "api" / "mcpRawConsoleClient.ts")
    workspace = _read(UI_DIR / "mcp" / "McpRawConsoleWorkspace.tsx")

    assert 'fetchJson<McpContractRegistry>("/mcp/contracts"' in client
    assert "executeMcpRawTool" in client
    assert "permissionFamilyRequiresConfirmation" in client
    assert 'family === "explicit_apply" || family === "destructive"' in client
    assert "fetchMcpContractRegistry" in workspace
    assert "executeMcpRawTool" in workspace
    assert "mcpRawConfirmationPhrase" in workspace
    assert "Request JSON" in workspace
    assert "Raw response" in workspace


def test_mcp_raw_console_does_not_import_paid_module_ui() -> None:
    workspace = _read(UI_DIR / "mcp" / "McpRawConsoleWorkspace.tsx")
    banned_fragments = [
        "CloneAppProductShell",
        "CloneAppChatRoute",
        "CloneAppTeachRoute",
        "ChatBrainWorkspace",
        "GrowBrainWorkspace",
        "MaintenanceBrainWorkspace",
        "../clone-app/",
        "../modules/CloneApp",
    ]

    assert all(fragment not in workspace for fragment in banned_fragments)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
