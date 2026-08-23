# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "agvm_cockpit_prototype" / "src" / "new-ui"


def _is_public_core_export() -> bool:
    return (ROOT / ".agvm-public-export-marker").exists()


def test_core_workspace_stage_does_not_import_paid_module_routes() -> None:
    if _is_public_core_export():
        source = _read(ROOT / "agvm_cockpit_prototype" / "src" / "App.tsx")
        assert "Use Detwin Cloud" in source
        assert "CloneAppProductShell" not in source
        assert "CloneAppChatRoute" not in source
        assert "MaintenanceBrainWorkspace" not in source
        return

    source = _read(UI_DIR / "shell" / "CoreModeWorkspaceStage.tsx")
    banned_fragments = [
        "CloneAppProductShell",
        "CloneAppChatRoute",
        "CloneAppTeachRoute",
        "ChatBrainWorkspace",
        "GrowBrainWorkspace",
        "MaintenanceBrainWorkspace",
        "../clone-app/",
    ]

    assert "LoadedHostModuleSlot" in source
    assert "GenericModuleWorkspace" in source
    assert all(fragment not in source for fragment in banned_fragments)


def test_shell_policy_defines_public_core_profile_from_classification() -> None:
    if _is_public_core_export():
        source = _read(ROOT / "agvm_cockpit_prototype" / "src" / "App.tsx")
        assert "AGVM Core" in source
        assert "Use Detwin Cloud" in source
        return

    source = _read(UI_DIR / "shell" / "coreShellPolicy.ts")

    assert 'CockpitShellProfile = "pro_monolith" | "public_core"' in source
    assert "cockpitModeClassifications.filter((item) => item.publicCoreAllowed)" in source
    assert "resolveCockpitModeForShellProfile" in source
    assert 'return "clone_app";' in source


def test_mode_rail_filters_static_modes_before_rendering_module_slots() -> None:
    if _is_public_core_export():
        source = _read(ROOT / "agvm_cockpit_prototype" / "src" / "App.tsx")
        assert "modules" in source
        assert "Grow" in source
        return

    source = _read(UI_DIR / "shell" / "ModeRail.tsx")

    assert "visibleModeKeys?: readonly CockpitModeKey[]" in source
    assert "visibleModeSet" in source
    assert "visibleModeGroups.map" in source
    assert "visibleHostModuleSlots(moduleSlots)" in source


def test_neural_cockpit_uses_public_core_stage_without_changing_default_profile() -> None:
    if _is_public_core_export():
        source = _read(ROOT / "agvm_cockpit_prototype" / "src" / "App.tsx")
        assert "Local AGVM" in source
        assert "Install module" not in source
        return

    source = _read(UI_DIR / "NeuralCockpitApp.tsx")

    assert "readCockpitShellProfile" in source
    assert "visibleModeKeys={visibleShellModeKeys}" in source
    assert 'shellProfile === "public_core"' in source
    assert "<CoreModeWorkspaceStage" in source
    assert "<ModeWorkspaceStage" in source
    assert "{...workspaceStageProps}" in source


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
