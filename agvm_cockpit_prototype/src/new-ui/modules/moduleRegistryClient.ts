import { fetchJson } from "../../api/client";

import type { HostModuleSlot } from "./moduleSlots";
import {
  moduleManifestIsReady,
  normalizeAgvmModuleManifest,
  type AgvmModuleManifest,
  type AgvmModuleUiMount,
} from "./moduleManifestContracts";

export type HostModuleRegistryDefinition = {
  fallbackDetail: string;
  label: string;
  manifestPath: string;
  moduleId: string;
  readyDetail: string;
  slotId: string;
  timeoutMs?: number;
};

export type LoadedHostModuleSlot = HostModuleSlot & {
  manifest: AgvmModuleManifest | null;
  uiMounts: AgvmModuleUiMount[];
};

export function defaultHostModuleSlot(definition: HostModuleRegistryDefinition): LoadedHostModuleSlot {
  return {
    badge: "Optional",
    detail: definition.fallbackDetail,
    devFixture: false,
    label: definition.label,
    licenseState: "unknown",
    manifest: null,
    moduleId: definition.moduleId,
    moduleState: "unknown",
    slotId: definition.slotId,
    state: "hidden",
    uiMounts: [],
  };
}

export async function loadHostModuleSlot(definition: HostModuleRegistryDefinition): Promise<LoadedHostModuleSlot> {
  try {
    const payload = await fetchJson<unknown>(definition.manifestPath, { timeoutMs: definition.timeoutMs ?? 4500 });
    const manifest = normalizeAgvmModuleManifest(payload);
    if (!manifest) {
      return disabledHostModuleSlot(definition, null, "Module manifest is not compatible with this AGVM core.");
    }
    return moduleManifestToHostSlot(manifest, definition);
  } catch {
    return defaultHostModuleSlot(definition);
  }
}

export function moduleManifestToHostSlot(
  manifest: AgvmModuleManifest,
  definition: HostModuleRegistryDefinition,
): LoadedHostModuleSlot {
  if (moduleManifestIsReady(manifest)) {
    return {
      badge: moduleManifestUsesDevFixture(manifest) ? "Dev fixture" : "Ready",
      detail: definition.readyDetail,
      devFixture: moduleManifestUsesDevFixture(manifest),
      label: definition.label,
      licenseState: manifest.license_state,
      manifest,
      moduleId: manifest.module_id || definition.moduleId,
      moduleState: manifest.module_state,
      slotId: definition.slotId,
      state: "ready",
      uiMounts: manifest.ui.mounts,
    };
  }
  if (manifest.module_state === "absent") {
    return {
      ...defaultHostModuleSlot(definition),
      detail: manifest.safe_fallback_message || definition.fallbackDetail,
      devFixture: moduleManifestUsesDevFixture(manifest),
      licenseState: manifest.license_state,
      manifest,
      moduleId: manifest.module_id || definition.moduleId,
      moduleState: manifest.module_state,
    };
  }
  return disabledHostModuleSlot(definition, manifest, manifest.safe_fallback_message || "Module is installed but not ready.");
}

function disabledHostModuleSlot(
  definition: HostModuleRegistryDefinition,
  manifest: AgvmModuleManifest | null,
  detail: string,
): LoadedHostModuleSlot {
  return {
    badge: moduleSlotBadge(manifest),
    detail,
    devFixture: moduleManifestUsesDevFixture(manifest),
    label: definition.label,
    licenseState: manifest?.license_state || "unknown",
    manifest,
    moduleId: manifest?.module_id || definition.moduleId,
    moduleState: manifest?.module_state || "unknown",
    slotId: definition.slotId,
    state: "disabled",
    uiMounts: [],
  };
}

function moduleSlotBadge(manifest: AgvmModuleManifest | null): string {
  if (!manifest) return "Unavailable";
  if (moduleManifestUsesDevFixture(manifest)) return "Dev fixture";
  if (manifest.module_state === "unlicensed") {
    if (manifest.license_state === "expired") return "Expired";
    if (manifest.license_state === "invalid") return "Invalid";
    return "Locked";
  }
  if (manifest.module_state === "incompatible") return "Incompatible";
  if (manifest.module_state === "degraded") return "Degraded";
  return "Unavailable";
}

function moduleManifestUsesDevFixture(manifest: AgvmModuleManifest | null): boolean {
  const diagnostics = manifest?.diagnostics || {};
  return diagnostics.dev_fixture === true || diagnostics.license_source === "unsigned_dev_fixture";
}
