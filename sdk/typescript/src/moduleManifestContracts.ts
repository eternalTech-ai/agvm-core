// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
// SPDX-FileContributor: Lorenzo Massaro
// SPDX-License-Identifier: Apache-2.0

export const AGVM_MODULE_MANIFEST_SCHEMA_VERSION = "agvm.module_manifest.v1";
export const LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION = "agvm.clone.module_manifest.v1";

export type AgvmModuleEdition = "absent" | "free_stub" | "paid";
export type AgvmModuleBackendStatus = "healthy" | "degraded" | "missing" | "incompatible";
export type AgvmModuleLicenseState = "installed" | "missing" | "expired" | "invalid" | "not_required";
export type AgvmModuleState = "absent" | "unlicensed" | "incompatible" | "degraded" | "healthy";
export type AgvmModuleUiKind = "none" | "local_route" | "remote_bundle" | "hosted_route";

export type AgvmModuleUiMount = {
  description: string;
  label: string;
  nav_group: string;
  path: string;
  required_capability: string;
  route_id: string;
};

export type AgvmModuleUiBundle = {
  entry_url: string | null;
  integrity: string | null;
  kind: AgvmModuleUiKind;
  mounts: AgvmModuleUiMount[];
};

export type AgvmModuleMcpTools = {
  adds_tools: string[];
  uses_core_tools: string[];
};

export type AgvmModuleLicense = {
  lease_expires_at: string | null;
  plan_required: string | null;
};

export type AgvmModuleManifest = {
  api_base_path: string;
  available: boolean;
  backend_status: AgvmModuleBackendStatus;
  capabilities: Record<string, boolean>;
  diagnostics: Record<string, unknown>;
  edition: AgvmModuleEdition;
  license: AgvmModuleLicense;
  license_state: AgvmModuleLicenseState;
  mcp_tools: AgvmModuleMcpTools;
  module_id: string;
  module_state: AgvmModuleState;
  module_version: string;
  safe_fallback_message: string;
  schema_version: typeof AGVM_MODULE_MANIFEST_SCHEMA_VERSION;
  source_schema_version?: string;
  ui: AgvmModuleUiBundle;
};

export type LegacyCloneAppModuleManifest = {
  api_base_path: string;
  apply_locked: true;
  available: boolean;
  backend_status: Exclude<AgvmModuleBackendStatus, never>;
  capabilities: Record<string, boolean>;
  diagnostics?: Record<string, unknown>;
  edition: AgvmModuleEdition;
  license_state: Exclude<AgvmModuleLicenseState, "not_required">;
  module_id: string;
  mutates_agvm_memory: false;
  mutation_allowed: false;
  safe_fallback_message: string;
  schema_version: typeof LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION;
  ui_mounts: AgvmModuleUiMount[];
  version: string;
};

const moduleEditions = new Set<AgvmModuleEdition>(["absent", "free_stub", "paid"]);
const backendStatuses = new Set<AgvmModuleBackendStatus>(["healthy", "degraded", "missing", "incompatible"]);
const licenseStates = new Set<AgvmModuleLicenseState>(["installed", "missing", "expired", "invalid", "not_required"]);
const moduleStates = new Set<AgvmModuleState>(["absent", "unlicensed", "incompatible", "degraded", "healthy"]);
const uiKinds = new Set<AgvmModuleUiKind>(["none", "local_route", "remote_bundle", "hosted_route"]);

export function normalizeAgvmModuleManifest(payload: unknown): AgvmModuleManifest | null {
  if (!isRecord(payload)) return null;
  const schemaVersion = readText(payload.schema_version);
  if (schemaVersion === AGVM_MODULE_MANIFEST_SCHEMA_VERSION) {
    return normalizeGenericModuleManifest(payload);
  }
  if (schemaVersion === LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION) {
    return normalizeLegacyCloneAppModuleManifest(payload);
  }
  return null;
}

export function deriveAgvmModuleState(input: {
  backend_status: AgvmModuleBackendStatus;
  edition: AgvmModuleEdition;
  license_state: AgvmModuleLicenseState;
}): AgvmModuleState {
  if (input.edition === "absent" || input.backend_status === "missing") return "absent";
  if (input.backend_status === "incompatible") return "incompatible";
  if (input.edition === "paid" && input.license_state !== "installed") return "unlicensed";
  if (input.license_state === "expired" || input.license_state === "invalid") return "unlicensed";
  if (input.backend_status === "degraded") return "degraded";
  return "healthy";
}

export function moduleManifestIsReady(manifest: AgvmModuleManifest | null): boolean {
  return Boolean(manifest && manifest.module_state === "healthy" && manifest.available);
}

function normalizeGenericModuleManifest(payload: Record<string, unknown>): AgvmModuleManifest | null {
  const edition = readAllowed(payload.edition, moduleEditions);
  const backendStatus = readAllowed(payload.backend_status, backendStatuses);
  const licenseState = readAllowed(payload.license_state, licenseStates);
  const uiPayload = isRecord(payload.ui) ? payload.ui : null;
  if (!edition || !backendStatus || !licenseState || !uiPayload) return null;
  const moduleState = deriveAgvmModuleState({
    backend_status: backendStatus,
    edition,
    license_state: licenseState,
  });
  const declaredState = readText(payload.module_state);
  if (declaredState && (!moduleStates.has(declaredState as AgvmModuleState) || declaredState !== moduleState)) return null;
  const manifest: AgvmModuleManifest = {
    api_base_path: normalizeApiBasePath(readText(payload.api_base_path)),
    available: Boolean(payload.available),
    backend_status: backendStatus,
    capabilities: readBooleanMap(payload.capabilities),
    diagnostics: isRecord(payload.diagnostics) ? payload.diagnostics : {},
    edition,
    license: readLicense(payload.license),
    license_state: licenseState,
    mcp_tools: readMcpTools(payload.mcp_tools),
    module_id: readText(payload.module_id),
    module_state: moduleState,
    module_version: readText(payload.module_version),
    safe_fallback_message: readText(payload.safe_fallback_message),
    schema_version: AGVM_MODULE_MANIFEST_SCHEMA_VERSION,
    source_schema_version: readText(payload.source_schema_version) || undefined,
    ui: readUiBundle(uiPayload),
  };
  return manifestIsCoherent(manifest) ? manifest : null;
}

function normalizeLegacyCloneAppModuleManifest(payload: Record<string, unknown>): AgvmModuleManifest | null {
  const edition = readAllowed(payload.edition, moduleEditions);
  const backendStatus = readAllowed(payload.backend_status, backendStatuses);
  const licenseState = readAllowed(payload.license_state, licenseStates);
  if (!edition || !backendStatus || !licenseState) return null;
  const mounts = readUiMounts(payload.ui_mounts);
  const moduleState = deriveAgvmModuleState({
    backend_status: backendStatus,
    edition,
    license_state: licenseState,
  });
  const manifest: AgvmModuleManifest = {
    api_base_path: normalizeApiBasePath(readText(payload.api_base_path)),
    available: Boolean(payload.available),
    backend_status: backendStatus,
    capabilities: readBooleanMap(payload.capabilities),
    diagnostics: {
      ...(isRecord(payload.diagnostics) ? payload.diagnostics : {}),
      legacy_schema_version: LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION,
      legacy_safety_flags: {
        apply_locked: Boolean(payload.apply_locked ?? true),
        mutates_agvm_memory: Boolean(payload.mutates_agvm_memory ?? false),
        mutation_allowed: Boolean(payload.mutation_allowed ?? false),
      },
    },
    edition,
    license: { lease_expires_at: null, plan_required: edition === "paid" ? "pro" : null },
    license_state: licenseState,
    mcp_tools: { adds_tools: [], uses_core_tools: ["retrieve_context", "write_memory_preview"] },
    module_id: readText(payload.module_id),
    module_state: moduleState,
    module_version: readText(payload.version),
    safe_fallback_message: readText(payload.safe_fallback_message),
    schema_version: AGVM_MODULE_MANIFEST_SCHEMA_VERSION,
    source_schema_version: LEGACY_CLONE_APP_MODULE_MANIFEST_SCHEMA_VERSION,
    ui: { entry_url: null, integrity: null, kind: mounts.length > 0 ? "local_route" : "none", mounts },
  };
  return manifestIsCoherent(manifest) ? manifest : null;
}

function manifestIsCoherent(manifest: AgvmModuleManifest): boolean {
  if (!manifest.module_id || !manifest.module_version || !manifest.api_base_path || !manifest.safe_fallback_message) return false;
  if (!manifest.api_base_path.startsWith("/")) return false;
  if (manifest.available !== (manifest.module_state === "healthy")) return false;
  if (manifest.edition === "paid" && !manifest.license.plan_required) return false;
  return manifest.ui.mounts.every((mount) => Boolean(manifest.capabilities[mount.required_capability]));
}

function readUiBundle(payload: Record<string, unknown>): AgvmModuleUiBundle {
  const kind = readAllowed(payload.kind, uiKinds) || "none";
  return {
    entry_url: readText(payload.entry_url) || null,
    integrity: readText(payload.integrity) || null,
    kind,
    mounts: readUiMounts(payload.mounts),
  };
}

function readUiMounts(value: unknown): AgvmModuleUiMount[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter(isRecord)
    .map((mount) => ({
      description: readText(mount.description),
      label: readText(mount.label),
      nav_group: readText(mount.nav_group),
      path: readText(mount.path),
      required_capability: readText(mount.required_capability),
      route_id: readText(mount.route_id),
    }))
    .filter((mount) => mount.route_id && mount.label && mount.path.startsWith("/") && mount.required_capability);
}

function readMcpTools(value: unknown): AgvmModuleMcpTools {
  if (!isRecord(value)) return { adds_tools: [], uses_core_tools: [] };
  return {
    adds_tools: readTextList(value.adds_tools),
    uses_core_tools: readTextList(value.uses_core_tools),
  };
}

function readLicense(value: unknown): AgvmModuleLicense {
  if (!isRecord(value)) return { lease_expires_at: null, plan_required: null };
  return {
    lease_expires_at: readText(value.lease_expires_at) || null,
    plan_required: readText(value.plan_required) || null,
  };
}

function readBooleanMap(value: unknown): Record<string, boolean> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([key]) => key.trim()).map(([key, enabled]) => [key.trim(), Boolean(enabled)]));
}

function readTextList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(readText).filter(Boolean);
}

function readAllowed<T extends string>(value: unknown, allowed: Set<T>): T | null {
  const clean = readText(value);
  return allowed.has(clean as T) ? (clean as T) : null;
}

function normalizeApiBasePath(value: string): string {
  if (!value) return "";
  return value.startsWith("/") ? value.replace(/\/+$/, "") || "/" : `/${value.replace(/^\/+|\/+$/g, "")}`;
}

function readText(value: unknown): string {
  return value == null ? "" : String(value).trim();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
