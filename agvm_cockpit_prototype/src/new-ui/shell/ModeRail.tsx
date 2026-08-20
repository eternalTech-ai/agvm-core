import {
  Activity,
  Braces,
  HeartPulse,
  KeyRound,
  Plug,
  Search,
  Settings,
  Sparkles,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { HostModuleSlot } from "../modules/moduleSlots";
import { visibleHostModuleSlots } from "../modules/moduleSlots";

export type CockpitModeKey =
  | "brain"
  | "retrieve"
  | "clone_app"
  | "chat"
  | "payload"
  | "paths"
  | "documents"
  | "grow"
  | "health"
  | "evolve"
  | "benchmarks"
  | "platform"
  | "mcp_setup"
  | "mcp_raw_console"
  | "settings";

type CockpitMode = {
  key: CockpitModeKey;
  label: string;
  icon: LucideIcon;
};

type CockpitModeGroup = {
  id: string;
  label: string;
  modes: CockpitMode[];
};

const cockpitModeGroups: CockpitModeGroup[] = [
  {
    id: "core",
    label: "Core",
    modes: [
      { key: "retrieve", label: "Context", icon: Search },
      { key: "payload", label: "Results", icon: Braces },
      { key: "health", label: "Health", icon: HeartPulse },
      { key: "benchmarks", label: "Bench", icon: Activity },
    ],
  },
  {
    id: "connect",
    label: "Connect",
    modes: [
      { key: "mcp_setup", label: "MCP Setup", icon: Plug },
      { key: "platform", label: "Detwin Account", icon: KeyRound },
      { key: "settings", label: "Advanced", icon: Settings },
    ],
  },
];

type ModeRailProps = {
  activeModuleSlotId?: string | null;
  activeMode: CockpitModeKey;
  liveModes?: Partial<Record<CockpitModeKey, "running" | "ready" | "pending">>;
  moduleSlots?: HostModuleSlot[];
  onModuleSlotOpen?: (slot: HostModuleSlot) => void;
  onModeChange: (mode: CockpitModeKey) => void;
  showLockedModuleSlots?: boolean;
  visibleModeKeys?: readonly CockpitModeKey[];
};

export function ModeRail({
  activeMode,
  activeModuleSlotId = null,
  liveModes = {},
  moduleSlots = [],
  onModeChange,
  onModuleSlotOpen,
  showLockedModuleSlots = false,
  visibleModeKeys,
}: ModeRailProps) {
  const visibleModuleSlots = showLockedModuleSlots ? moduleSlots : visibleHostModuleSlots(moduleSlots);
  const visibleModeSet = visibleModeKeys ? new Set(visibleModeKeys) : null;
  const visibleModeGroups = visibleModeSet
    ? cockpitModeGroups
        .map((group) => ({ ...group, modes: group.modes.filter((mode) => visibleModeSet.has(mode.key)) }))
        .filter((group) => group.modes.length)
    : cockpitModeGroups;
  return (
    <aside className="et-rail" aria-label="AGVM modes">
      {visibleModeGroups.map((group) => (
        <div className="rail-group" key={group.id}>
          <span className="rail-group-label">{group.label}</span>
          {group.modes.map((mode) => {
            const Icon = mode.icon;
            const liveTone = liveModes[mode.key];
            const label = displayModeLabel(mode, activeModuleSlotId);
            return (
              <button
                aria-current={activeMode === mode.key ? "page" : undefined}
                aria-label={label}
                className={[activeMode === mode.key ? "active" : "", liveTone ? `mode-live mode-live-${liveTone}` : ""].filter(Boolean).join(" ")}
                key={mode.key}
                onClick={() => onModeChange(mode.key)}
                title={label}
                type="button"
              >
                <Icon size={16} />
                <span>{label}</span>
                {liveTone ? <i aria-hidden="true" /> : null}
                {liveTone === "running" ? <em aria-hidden="true">Live</em> : null}
              </button>
            );
          })}
        </div>
      ))}
      {visibleModuleSlots.length ? (
        <div className="rail-group rail-module-group">
          <span className="rail-group-label">Modules</span>
          {visibleModuleSlots.map((slot) => {
            const state = moduleDisplayState(slot, activeModuleSlotId === slot.slotId);
            return (
              <button
                aria-current={activeModuleSlotId === slot.slotId ? "page" : undefined}
                aria-label={`${slot.label}: ${state}`}
                className={[
                  `rail-module-slot rail-module-slot-${slot.state}`,
                  `rail-module-state-${state}`,
                  activeModuleSlotId === slot.slotId ? "active" : "",
                ].filter(Boolean).join(" ")}
                disabled={slot.state !== "ready" || !onModuleSlotOpen}
                key={slot.slotId}
                onClick={() => onModuleSlotOpen?.(slot)}
                title={`${moduleStateLabel(state)} - ${slot.detail}`}
                type="button"
              >
                <Sparkles size={16} />
                <span>{slot.label}</span>
                <em>{moduleStateLabel(state)}</em>
              </button>
            );
          })}
        </div>
      ) : null}
    </aside>
  );
}

function displayModeLabel(mode: CockpitMode, activeModuleSlotId?: string | null) {
  if (activeModuleSlotId === "clone_app") {
    if (mode.key === "mcp_setup") return "Connect";
    if (mode.key === "mcp_raw_console") return "Raw Tools";
  }
  return mode.label;
}

function moduleDisplayState(slot: HostModuleSlot, active: boolean) {
  if (slot.state === "ready" && active) return "running";
  if (slot.state === "ready") return "installed";
  if (slot.state === "disabled" && (slot.moduleState === "unlicensed" || ["missing", "expired", "invalid"].includes(slot.licenseState || ""))) {
    return "locked";
  }
  if (slot.state === "disabled") return "error";
  return "locked";
}

function moduleStateLabel(state: ReturnType<typeof moduleDisplayState>) {
  if (state === "running") return "Running";
  if (state === "installed") return "Installed";
  if (state === "error") return "Error";
  return "Locked";
}
