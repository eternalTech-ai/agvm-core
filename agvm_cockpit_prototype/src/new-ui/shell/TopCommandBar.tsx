import { Brain } from "lucide-react";
import { useMemo } from "react";

import type { AgvmBrainSummary } from "../api/agvmClient";
import { Dropdown, type DropdownOption } from "../components/Dropdown";

export type StatusTone = "ready" | "active" | "pending" | "neutral" | "degraded";

export type TopCommandBarViewModel = {
  activeBrainId: string;
  activeBrainLabel: string;
  brains: AgvmBrainSummary[];
  brainRegistryStatus: "loading" | "ready" | "error";
  brainSwitching: boolean;
  backend: {
    value: string;
    label: string;
    tone: StatusTone;
  };
  graph: {
    value: string;
    label: string;
    tone: StatusTone;
  };
  runState: {
    value: string;
    label: string;
    tone: StatusTone;
  };
  activeMode: string;
};

type TopCommandBarProps = {
  view: TopCommandBarViewModel;
  onBrainChange: (brainId: string) => void;
};

export function TopCommandBar({ onBrainChange, view }: TopCommandBarProps) {
  return (
    <header className="et-topbar cockpit-command-header">
      <div className="et-brand">
        <BrandMark />
        <div className="brand-copy">
          <strong>AGVM</strong>
          <span>Powered by Eternal Tech</span>
        </div>
      </div>
      <BrainSelector view={view} onBrainChange={onBrainChange} />
      <section className="topbar-status-strip" aria-label="Cockpit status">
        <StatusTile label={view.backend.label} value={view.backend.value} tone={view.backend.tone} />
        <StatusTile label={view.graph.label} value={view.graph.value} tone={view.graph.tone} />
        <StatusTile label="Mode" value={view.activeMode} tone="active" />
        <StatusTile label={view.runState.label} value={view.runState.value} tone={view.runState.tone} />
      </section>
    </header>
  );
}

function BrainSelector({ onBrainChange, view }: TopCommandBarProps) {
  const disabled = view.brainSwitching;
  const activeBrainId = view.activeBrainId || "";
  const visibleBrains = useMemo(
    () =>
      activeBrainId && !view.brains.some((brain) => brainIdOf(brain) === activeBrainId)
        ? [{ brain_id: activeBrainId, display_name: view.activeBrainLabel }, ...view.brains]
        : view.brains,
    [activeBrainId, view.activeBrainLabel, view.brains],
  );
  const fallbackValue = activeBrainId || "__none";
  const options = useMemo<DropdownOption<string>[]>(() => {
    if (!visibleBrains.length) {
      return [{ value: fallbackValue, label: statusLabel(view), meta: view.brainRegistryStatus === "loading" ? "loading registry" : "registry unavailable" }];
    }
    return visibleBrains.map((brain) => {
      const brainId = brainIdOf(brain);
      return {
        value: brainId || `missing:${brainDisplayName(brain)}`,
        label: brainDisplayName(brain),
        meta: brainMeta(brain, view.activeMode),
        disabled: !brainId,
      };
    });
  }, [fallbackValue, view, visibleBrains]);

  return (
    <Dropdown
      ariaLabel="Active brain"
      className={`brain-selector ${view.brainRegistryStatus} ${view.brainSwitching ? "switching" : ""}`}
      disabled={disabled}
      icon={<Brain size={15} />}
      label="Active brain"
      menuWidth="wide"
      onChange={(brainId) => {
        if (brainId && brainId !== "__none" && brainId !== activeBrainId) onBrainChange(brainId);
      }}
      options={options}
      value={options.some((option) => option.value === activeBrainId) ? activeBrainId : options[0]?.value || fallbackValue}
    />
  );
}

function StatusTile({ label, value, tone }: { label: string; value: string; tone?: StatusTone }) {
  return (
    <article className={`status-tile ${tone || "neutral"}`} title={`${label}: ${value}`} aria-label={`${label}: ${value}`}>
      <i className={tone || "neutral"} />
      <div>
        <strong>{value}</strong>
        <span>{label}</span>
      </div>
    </article>
  );
}

function brainIdOf(brain: AgvmBrainSummary) {
  return String(brain.brain_id || brain.id || "");
}

function brainDisplayName(brain: AgvmBrainSummary) {
  const label = String(brain.display_name || brain.name || brain.brain_id || brain.id || "Unnamed brain").trim();
  return label || "Unnamed brain";
}

function brainMeta(brain: AgvmBrainSummary, activeMode: string) {
  const id = brainIdOf(brain);
  const nodeCount = typeof brain.node_count === "number" ? `${brain.node_count.toLocaleString()} nodes` : "node count unavailable";
  const cloneAppMode = activeMode === "clone_app" || activeMode === "Clone App";
  const safe =
    cloneAppMode
      ? brain.safe_for_mcp === false
        ? "memory gated"
        : "memory ready"
      : brain.safe_for_mcp === false
        ? "MCP gated"
        : "MCP ready";
  return id ? `${id} / ${nodeCount} / ${safe}` : `${nodeCount} / ${safe}`;
}

function statusLabel(view: TopCommandBarViewModel) {
  if (view.brainRegistryStatus === "loading") return "Loading brain registry";
  if (view.brainRegistryStatus === "error") return view.activeBrainId ? `Active: ${view.activeBrainLabel}` : "Brain registry unavailable";
  return view.activeBrainLabel || "No active brain";
}

const brandLogoSource = "/brand/logo-primary.png";

function BrandMark() {
  return (
    <span className="et-mark et-mark-logo" aria-hidden="true">
      <img alt="" className="et-logo-image" src={brandLogoSource} />
    </span>
  );
}
