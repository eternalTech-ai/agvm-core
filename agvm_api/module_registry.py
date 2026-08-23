# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Static AGVM module registry helpers.

This is the local, dependency-free registry used before the platform registry
exists. It stores normalized manifests only; it never imports paid module code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - supports both package and top-level test imports.
    from .module_manifest_contracts import (
        AgvmModuleManifest,
        ModuleManifestValidationError,
        ModuleState,
        normalize_module_manifest,
    )
except ImportError:  # pragma: no cover
    from module_manifest_contracts import (  # type: ignore[no-redef]
        AgvmModuleManifest,
        ModuleManifestValidationError,
        ModuleState,
        normalize_module_manifest,
    )


@dataclass(frozen=True)
class StaticModuleRegistrySummary:
    total: int
    by_state: dict[ModuleState, int]
    healthy_module_ids: list[str]
    unavailable_module_ids: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_state": dict(self.by_state),
            "healthy_module_ids": list(self.healthy_module_ids),
            "unavailable_module_ids": list(self.unavailable_module_ids),
        }


class StaticModuleRegistry:
    """In-memory registry for already-discovered module manifests."""

    def __init__(self, manifests: Iterable[Mapping[str, Any] | AgvmModuleManifest] = ()) -> None:
        self._manifests: dict[str, AgvmModuleManifest] = {}
        for manifest in manifests:
            self.register(manifest)

    def register(self, payload: Mapping[str, Any] | AgvmModuleManifest) -> AgvmModuleManifest:
        manifest = normalize_module_manifest(payload)
        if manifest.module_id in self._manifests:
            raise ModuleManifestValidationError(f"duplicate_module_manifest:{manifest.module_id}")
        self._manifests[manifest.module_id] = manifest
        return manifest

    def get_manifest(self, module_id: str) -> AgvmModuleManifest | None:
        return self._manifests.get(str(module_id).strip())

    def require_manifest(self, module_id: str) -> AgvmModuleManifest:
        manifest = self.get_manifest(module_id)
        if manifest is None:
            raise KeyError(f"module_manifest_not_registered:{module_id}")
        return manifest

    def list_manifests(self, *, include_absent: bool = True) -> list[AgvmModuleManifest]:
        manifests = sorted(self._manifests.values(), key=lambda manifest: manifest.module_id)
        if include_absent:
            return manifests
        return [manifest for manifest in manifests if manifest.module_state != "absent"]

    def list_public_manifests(self, *, include_absent: bool = True) -> list[dict[str, Any]]:
        return [manifest.as_dict() for manifest in self.list_manifests(include_absent=include_absent)]

    def summary(self) -> StaticModuleRegistrySummary:
        by_state: dict[ModuleState, int] = {
            "absent": 0,
            "unlicensed": 0,
            "incompatible": 0,
            "degraded": 0,
            "healthy": 0,
        }
        healthy: list[str] = []
        unavailable: list[str] = []
        for manifest in self.list_manifests():
            by_state[manifest.module_state] += 1
            if manifest.module_state == "healthy":
                healthy.append(manifest.module_id)
            else:
                unavailable.append(manifest.module_id)
        return StaticModuleRegistrySummary(
            total=len(self._manifests),
            by_state=by_state,
            healthy_module_ids=healthy,
            unavailable_module_ids=unavailable,
        )


def build_static_module_registry(
    manifests: Iterable[Mapping[str, Any] | AgvmModuleManifest],
) -> StaticModuleRegistry:
    return StaticModuleRegistry(manifests)
