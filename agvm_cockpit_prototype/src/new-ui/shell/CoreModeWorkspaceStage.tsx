import {
  AlertTriangle,
  Braces,
  Database,
  FileText,
  PackageCheck,
  PackageX,
  type LucideIcon,
} from "lucide-react";

import type { BrainRenderNode } from "../brain/brainSceneModel";
import { BenchmarkBrainWorkspace } from "../bench/BenchmarkBrainWorkspace";
import { McpRawConsoleWorkspace } from "../mcp/McpRawConsoleWorkspace";
import type { LoadedHostModuleSlot } from "../modules/moduleRegistryClient";
import { HealthBrainWorkspace } from "../ops/HealthBrainWorkspace";
import type { OpsWorkspaceContext, OpsWorkspaceProps } from "../ops/opsWorkspaceTypes";
import { McpSetupWorkspace } from "../settings/McpSetupWorkspace";
import { SettingsBrainWorkspace } from "../settings/SettingsBrainWorkspace";
import { ProductPageFrame, type ProductMetric } from "./ProductPageFrame";

type CoreModeWorkspaceStageProps = OpsWorkspaceProps & {
  activeModuleSlotId?: string | null;
  moduleSlots?: LoadedHostModuleSlot[];
};

type WorkspaceContext = OpsWorkspaceContext;

export function CoreModeWorkspaceStage(props: CoreModeWorkspaceStageProps) {
  const context = createCoreWorkspaceContext(props);

  if (props.mode === "payload" || props.mode === "paths" || props.mode === "documents") {
    return <CoreResultsWorkspace context={context} mode={props.mode} />;
  }
  if (props.mode === "health") return <HealthBrainWorkspace context={context} />;
  if (props.mode === "benchmarks") return <BenchmarkBrainWorkspace context={context} />;
  if (props.mode === "mcp_setup") return <McpSetupWorkspace context={context} />;
  if (props.mode === "mcp_raw_console") return <McpRawConsoleWorkspace context={context} />;
  if (props.mode === "settings") return <SettingsBrainWorkspace context={context} />;

  const activeSlot = props.moduleSlots?.find((slot) => slot.slotId === props.activeModuleSlotId) || null;
  if (activeSlot) return <GenericModuleWorkspace slot={activeSlot} />;

  return <BlockedNonCoreWorkspace mode={props.mode} />;
}

function CoreResultsWorkspace({ context, mode }: { context: WorkspaceContext; mode: string }) {
  const mission = context.mission;
  const running = context.liveRun.running || mission?.status === "running";
  const documentCount = mission?.documentRefs.length || 0;
  const routeCount = context.routeSegments.length;
  const rawReady = Boolean(mission?.payloadMarkdown || context.liveRun.events.length || mission?.live);
  const metrics: ProductMetric[] = [
    {
      label: "Context",
      value: mission ? (running ? "Building" : "Ready") : "Empty",
      detail: mission?.query || "Run Context first",
    },
    {
      label: "Documents",
      value: `${documentCount}`,
      detail: documentCount ? "refs exposed by MCP" : "no refs yet",
    },
    {
      label: "Routes",
      value: `${routeCount}`,
      detail: routeCount ? "path proof available" : "no path proof",
    },
  ];
  return (
    <ProductPageFrame
      actions={[]}
      className="core-results-frame"
      eyebrow="Core"
      icon={Braces}
      intent="Open-core result reader for context, documents and route proof."
      metrics={metrics}
      mode={mode}
      status={running ? "running" : mission ? "ready" : "waiting"}
      title="Run Results"
    >
      <section className="core-shell-fallback-grid">
        <CoreResultPanel
          icon={Braces}
          label="Context package"
          value={mission?.summary.clientCanProceed || mission?.summary.payloadState || "No context run selected."}
        />
        <CoreResultPanel
          icon={FileText}
          label="Document refs"
          value={documentCount ? `${documentCount} refs available for hydration.` : "No document evidence exposed yet."}
        />
        <CoreResultPanel
          icon={Database}
          label="Raw receipt"
          value={rawReady ? mission?.id || "stream payload available" : "Raw payload appears after a live MCP run."}
        />
      </section>
    </ProductPageFrame>
  );
}

function GenericModuleWorkspace({ slot }: { slot: LoadedHostModuleSlot }) {
  const manifest = slot.manifest;
  const mountCount = manifest?.ui.mounts.length || slot.uiMounts.length;
  const capabilityCount = manifest ? Object.values(manifest.capabilities).filter(Boolean).length : 0;
  const metrics: ProductMetric[] = [
    { label: "Module", value: slot.moduleId, detail: slot.badge },
    { label: "State", value: slot.state, detail: manifest?.module_state || "slot only" },
    { label: "Mounts", value: `${mountCount}`, detail: `${capabilityCount} enabled capabilities` },
  ];
  return (
    <ProductPageFrame
      actions={[]}
      className="core-module-host-frame"
      eyebrow="Module"
      icon={slot.state === "ready" ? PackageCheck : PackageX}
      intent="This module was discovered from a manifest. The public core host does not import paid product route code."
      metrics={metrics}
      mode={slot.slotId}
      status={moduleStatusLabel(slot)}
      title={slot.label}
    >
      <section className="core-shell-fallback-grid">
        <CoreResultPanel icon={PackageCheck} label="Manifest" value={manifest ? `${manifest.schema_version} / ${manifest.module_version}` : "No manifest loaded."} />
        <CoreResultPanel icon={AlertTriangle} label="Host behavior" value={moduleHostBehavior(slot)} />
        <CoreResultPanel icon={Database} label="API base" value={manifest?.api_base_path || "Not advertised."} />
      </section>
    </ProductPageFrame>
  );
}

function BlockedNonCoreWorkspace({ mode }: { mode: string }) {
  return (
    <ProductPageFrame
      actions={[]}
      className="core-blocked-mode-frame"
      eyebrow="Core"
      icon={PackageX}
      intent="This mode is not part of the public core shell. It must be supplied by a licensed module manifest."
      metrics={[]}
      mode="blocked"
      status="not mounted"
      title="Module Required"
    >
      <section className="core-shell-fallback-grid">
        <CoreResultPanel icon={PackageX} label="Requested mode" value={mode} />
        <CoreResultPanel icon={AlertTriangle} label="Boundary" value="No hard-coded paid module route is rendered by the public core shell." />
      </section>
    </ProductPageFrame>
  );
}

function CoreResultPanel({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <article className="product-state-card">
      <span>
        <Icon size={15} />
        {label}
      </span>
      <strong title={value}>{value}</strong>
    </article>
  );
}

function moduleStatusLabel(slot: LoadedHostModuleSlot) {
  if (slot.state === "ready") return "manifest ready";
  if (slot.state === "disabled") return "manifest unavailable";
  return "not installed";
}

function moduleHostBehavior(slot: LoadedHostModuleSlot) {
  const kind = slot.manifest?.ui.kind;
  if (kind === "remote_bundle") return "Ready for a remote UI bundle once the loader is enabled.";
  if (kind === "hosted_route") return "Ready for a hosted module route in AGVM Cloud.";
  if (kind === "local_route") return "Detected through legacy local routes; public core keeps paid route code out of this stage.";
  return slot.detail || "No UI mount advertised.";
}

function createCoreWorkspaceContext(props: OpsWorkspaceProps): WorkspaceContext {
  const byId = new Map(props.model.graphNodes.map((node) => [node.id, node]));
  const nodeList = (ids: string[] | undefined) => (ids || []).map((id) => byId.get(id)).filter((node): node is BrainRenderNode => Boolean(node));
  return {
    ...props,
    graphNodeCount: props.model.graphNodes.length,
    totalNodeCount: Number(props.graphMeta?.total_node_count || props.model.graphNodes.length),
    payloadNodes: nodeList(props.mission?.payloadNodeIds),
    landingNodes: nodeList(props.mission?.landingNodeIds),
    documentNodes: nodeList(props.mission?.documentNodeIds),
    routeSegments: props.mission?.routeSegments || [],
  };
}
