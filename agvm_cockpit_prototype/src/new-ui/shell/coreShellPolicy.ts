import type { HostModuleSlot } from "../modules/moduleSlots";
import { cockpitModeClassifications, isPublicCoreCockpitMode } from "../modules/coreModeClassification";
import type { CockpitModeKey } from "./ModeRail";

export type CockpitShellProfile = "pro_monolith" | "public_core";

export const DEFAULT_COCKPIT_SHELL_PROFILE: CockpitShellProfile = "pro_monolith";
export const PUBLIC_CORE_DEFAULT_MODE: CockpitModeKey = "retrieve";

export function normalizeCockpitShellProfile(value: string | null | undefined): CockpitShellProfile {
  const clean = String(value || "").trim().toLowerCase();
  if (clean === "public_core" || clean === "open_core" || clean === "core") return "public_core";
  return DEFAULT_COCKPIT_SHELL_PROFILE;
}

export function readCockpitShellProfile(): CockpitShellProfile {
  const params = typeof window !== "undefined" ? new URLSearchParams(window.location.search) : null;
  const queryValue = params?.get("shell_profile") || params?.get("agvm_shell");
  return normalizeCockpitShellProfile(queryValue || import.meta.env.VITE_AGVM_UI_SHELL_PROFILE);
}

export function cockpitModeKeysForShellProfile(profile: CockpitShellProfile): CockpitModeKey[] {
  if (profile === "public_core") {
    return cockpitModeClassifications.filter((item) => item.publicCoreAllowed).map((item) => item.mode);
  }
  return cockpitModeClassifications.map((item) => item.mode);
}

export function cockpitModeIsVisibleInShellProfile(profile: CockpitShellProfile, mode: CockpitModeKey): boolean {
  if (profile === "public_core") return isPublicCoreCockpitMode(mode);
  return true;
}

export function canActivateCockpitModeInShellProfile(
  profile: CockpitShellProfile,
  mode: CockpitModeKey,
  moduleSlots: HostModuleSlot[] = [],
): boolean {
  if (cockpitModeIsVisibleInShellProfile(profile, mode)) return true;
  if (profile !== "public_core") return true;
  return moduleSlots.some((slot) => slot.state === "ready" && moduleModeForSlot(slot) === mode);
}

export function resolveCockpitModeForShellProfile(
  profile: CockpitShellProfile,
  requestedMode: CockpitModeKey,
  moduleSlots: HostModuleSlot[] = [],
): CockpitModeKey {
  return canActivateCockpitModeInShellProfile(profile, requestedMode, moduleSlots)
    ? requestedMode
    : PUBLIC_CORE_DEFAULT_MODE;
}

function moduleModeForSlot(slot: HostModuleSlot): CockpitModeKey | null {
  if (slot.slotId === "clone_app" || slot.moduleId === "agvm_clone_app") return "clone_app";
  if (slot.slotId === "grow" || slot.moduleId === "agvm_grow_studio") return "grow";
  if (slot.slotId === "maintain" || slot.moduleId === "agvm_maintain_studio") return "evolve";
  return null;
}
