// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
// SPDX-FileContributor: Lorenzo Massaro
// SPDX-License-Identifier: Apache-2.0

export type HostModuleSlotState = "hidden" | "disabled" | "ready";
export type HostModuleLicenseState = "installed" | "missing" | "expired" | "invalid" | "not_required" | "unknown";
export type HostModuleRuntimeState = "absent" | "unlicensed" | "incompatible" | "degraded" | "healthy" | "unknown";

export type HostModuleSlot = {
  badge: string;
  detail: string;
  devFixture?: boolean;
  label: string;
  licenseState?: HostModuleLicenseState;
  moduleId: string;
  moduleState?: HostModuleRuntimeState;
  slotId: string;
  state: HostModuleSlotState;
};

export function visibleHostModuleSlots(slots: HostModuleSlot[]): HostModuleSlot[] {
  return slots.filter((slot) => slot.state === "ready");
}
