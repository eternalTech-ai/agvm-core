import { FileText, Loader2, RefreshCw, ShieldCheck } from "lucide-react";

import type { BrainRenderNode } from "../brain/brainSceneModel";
import type { MissionDocumentRef, MissionHydratedDocument, MissionProjection } from "../mission/missionProjection";
import type { CockpitModeKey } from "./ModeRail";

export const proofTabs = ["Payload", "Route", "Docs", "Memory", "Trace", "Validation"] as const;

export type ProofTab = (typeof proofTabs)[number];

export type ProofDockViewModel = {
  activeTab: ProofTab;
  activeMode: CockpitModeKey;
  mission: MissionProjection | null;
  clientView: boolean;
  selectedNode: BrainRenderNode | null;
  documentAnchorCount: number;
  graphError: string | null;
  retrieveError: string | null;
  inspectError: string | null;
  inspectRunning: boolean;
  projectionDiagnostics: MissionProjectionDiagnostics;
  documentHydration: MissionDocumentHydrationState;
};

export type MissionProjectionDiagnostics = {
  rawNodeCount: number;
  rawEdgeCount: number;
  rawPathCount: number;
  coordinateAnchorCount: number;
  graphMatchedNodeCount: number;
  projectionOnlyAnchorCount: number;
  drawableRouteSegmentCount: number;
};

export type MissionDocumentHydrationState = {
  error: string | null;
  hydratedDocuments: Record<string, MissionHydratedDocument>;
  runningDocumentId: string | null;
  selectedDocumentRefId: string | null;
};

type ProofDockProps = {
  view: ProofDockViewModel;
  onTabChange: (tab: ProofTab) => void;
  onClientViewChange: (enabled: boolean) => void;
  onDocumentRefSelect: (documentRefId: string) => void;
  onHydrateDocument: (ref: MissionDocumentRef) => void;
  onInspectFinal: () => void;
};

export function ProofDock({ view, onTabChange, onClientViewChange, onDocumentRefSelect, onHydrateDocument, onInspectFinal }: ProofDockProps) {
  const {
    activeTab,
    activeMode,
    mission,
    clientView,
    selectedNode,
    documentAnchorCount,
    graphError,
    retrieveError,
    inspectError,
    inspectRunning,
    projectionDiagnostics,
    documentHydration,
  } = view;
  const canInspectFinal = Boolean(mission && mission.source === "live_mcp" && mission.status !== "running" && !inspectRunning);
  const hydratedCount = mission ? mission.documentRefs.filter((ref) => documentHydration.hydratedDocuments[ref.id]).length : 0;

  return (
    <aside className="proof-dock">
      <div className="proof-title">
        <span>MCP proof dock</span>
        <h2>{proofDockTitle(activeMode, mission)}</h2>
      </div>
      <div className="proof-run-banner">
        <span>{mission ? missionSourceLabel(mission) : retrieveError ? "Context request error" : modeProofState(activeMode)}</span>
        <strong>{mission?.query || retrieveError || modeProofFallback(activeMode)}</strong>
      </div>
      <div className="proof-tabs">
        {proofTabs.map((tab) => (
          <button className={activeTab === tab ? "active" : ""} key={tab} onClick={() => onTabChange(tab)} type="button">
            {tab}
          </button>
        ))}
      </div>
      <div className="truth-ledger">
        <TruthRow label="Payload state" value={mission?.summary.payloadState || "No active run"} />
        <TruthRow label="Landing nodes" value={mission ? `${mission.landingNodeIds.length} selected` : "not requested"} />
        <TruthRow label="Client can proceed" value={mission?.summary.clientCanProceed || "waiting"} />
        <TruthRow label="Search signals" value={mission?.summary.semanticAi || "not started"} />
        <TruthRow label="Spatial layer" value={mission?.summary.spatialAi || "not started"} />
        <TruthRow label="Path truth" value={mission?.summary.pathTruth || "none"} />
        <TruthRow label="Run projection" value={projectionLedgerLabel(projectionDiagnostics)} />
        <TruthRow label="Documents" value={mission?.summary.documents || noMissionDocumentsLabel(activeMode, documentAnchorCount)} />
        <TruthRow label="Hydrated raw" value={mission ? `${hydratedCount}/${mission.documentRefs.length}` : "none"} />
        <TruthRow label="Inspect state" value={inspectStateLabel(mission, inspectRunning, inspectError)} />
        <TruthRow label="Missing reasons" value={documentHydration.error || inspectError || retrieveError || (graphError ? "graph unavailable" : mission?.summary.missingReasons || "No context run yet")} />
      </div>
      {activeTab === "Docs" && mission ? (
        <DocumentRefsPanel
          hydration={documentHydration}
          mission={mission}
          onHydrateDocument={onHydrateDocument}
          onSelectRef={onDocumentRefSelect}
        />
      ) : (
        <div className="payload-preview">
          {proofPanelLines(activeTab, mission, selectedNode, retrieveError, inspectError, projectionDiagnostics).map((line, index) => (
            <div key={`${activeTab}:${index}`}>{line || " "}</div>
          ))}
        </div>
      )}
      <div className="proof-action-row">
        <button className="proof-action inspect-final" disabled={!canInspectFinal} onClick={onInspectFinal} type="button">
          <RefreshCw size={15} />
          {inspectActionLabel(mission, inspectRunning)}
        </button>
        <button className={clientView ? "client-view active" : "client-view"} disabled={!mission} onClick={() => onClientViewChange(!clientView)} type="button">
          <ShieldCheck size={15} />
          {clientView ? "Exit Client View" : mission ? "Open Client View" : "Client View Unavailable"}
        </button>
      </div>
    </aside>
  );
}

function DocumentRefsPanel({
  hydration,
  mission,
  onHydrateDocument,
  onSelectRef,
}: {
  hydration: MissionDocumentHydrationState;
  mission: MissionProjection;
  onHydrateDocument: (ref: MissionDocumentRef) => void;
  onSelectRef: (documentRefId: string) => void;
}) {
  const refs = mission.documentRefs;
  const selectedRef = refs.find((ref) => ref.id === hydration.selectedDocumentRefId) || refs[0] || null;
  const hydratedDocument = selectedRef ? hydration.hydratedDocuments[selectedRef.id] : null;

  if (!refs.length) {
    return (
      <div className="payload-preview document-proof-panel">
        <div className="document-panel-empty">
          <FileText size={18} />
          <strong>No document refs selected.</strong>
          <span>The current MCP payload did not expose actionable document references.</span>
        </div>
      </div>
    );
  }

  return (
    <div className="payload-preview document-proof-panel">
      <div className="document-panel-header">
        <div>
          <span>Document References</span>
          <strong>{refs.length} actionable refs</strong>
        </div>
        <small>{mission.refsPolicy} policy - raw loads only through Hydrate</small>
      </div>
      <div className="document-ref-list">
        {refs.slice(0, 8).map((ref) => {
          const isSelected = selectedRef?.id === ref.id;
          const isHydrated = Boolean(hydration.hydratedDocuments[ref.id]);
          const isRunning = hydration.runningDocumentId === ref.id;
          return (
            <button className={isSelected ? "document-ref-card active" : "document-ref-card"} key={ref.id} onClick={() => onSelectRef(ref.id)} type="button">
              <span className="document-ref-rank">{ref.rank ? `#${ref.rank}` : "ref"}</span>
              <strong>{ref.title}</strong>
              <small>{documentRefSubtitle(ref)}</small>
              <span className="document-ref-status">
                {ref.rawAvailable ? "raw available" : "metadata only"}
                {ref.rawTextCharCount ? ` - ${ref.rawTextCharCount.toLocaleString()} chars` : ""}
                {isHydrated ? " - hydrated" : ""}
                {isRunning ? " - loading" : ""}
              </span>
            </button>
          );
        })}
      </div>
      {selectedRef ? (
        <div className="document-selected-panel">
          <div className="document-selected-header">
            <div>
              <span>Selected ref</span>
              <strong>{selectedRef.title}</strong>
              <small>{selectedRef.documentId || selectedRef.anchorNodeId || selectedRef.id}</small>
            </div>
            <button
              className="proof-action document-hydrate-action"
              disabled={!selectedRef.rawAvailable || hydration.runningDocumentId === selectedRef.id}
              onClick={() => onHydrateDocument(selectedRef)}
              type="button"
            >
              {hydration.runningDocumentId === selectedRef.id ? <Loader2 size={14} /> : <FileText size={14} />}
              {hydration.runningDocumentId === selectedRef.id ? "Hydrating" : hydratedDocument ? "Refresh Raw" : "Hydrate"}
            </button>
          </div>
          <div className="document-ref-details">
            <DetailRow label="Why" value={selectedRef.whyIncluded.join("; ") || selectedRef.relationshipToQuery || "backend did not provide a narrative reason"} />
            <DetailRow label="Expected" value={selectedRef.expectedSummary || "not reported"} />
            <DetailRow label="Matched" value={selectedRef.matchedTerms.join(", ") || "not reported"} />
            <DetailRow label="Recipe" value={hydrateRecipeLabel(selectedRef)} />
          </div>
          {hydration.error ? <div className="document-hydration-error">{hydration.error}</div> : null}
          {hydratedDocument ? (
            <div className="document-raw-viewer">
              <div className="document-raw-meta">
                <span>{hydratedDocument.status}</span>
                <span>{hydratedDocument.rawTextIncludedCharCount.toLocaleString()} / {hydratedDocument.rawTextCharCount.toLocaleString()} chars</span>
                <span>{hydratedDocument.rawTextTruncated ? "truncated" : "complete"}</span>
              </div>
              <pre>{hydratedDocument.rawText || "No raw text returned by retrieve_document."}</pre>
            </div>
          ) : (
            <div className="document-raw-placeholder">Raw source body is not loaded in the context package. Use Hydrate to call `retrieve_document` explicitly.</div>
          )}
        </div>
      ) : null}
    </div>
  );
}

function documentRefSubtitle(ref: MissionDocumentRef) {
  const parts = [ref.sourceLabel, ref.sourceType, ref.relationshipToQuery].filter(Boolean);
  if (typeof ref.score === "number") parts.push(`score ${ref.score.toFixed(3)}`);
  return parts.join(" - ") || ref.documentId || "document ref";
}

function hydrateRecipeLabel(ref: MissionDocumentRef) {
  const body = ref.hydrate.body;
  return `${ref.hydrate.endpoint} document_id=${body.document_id || "not provided"} policy=${body.document_text_policy}`;
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="document-detail-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function proofPanelLines(
  tab: ProofTab,
  mission: MissionProjection | null,
  selectedNode: BrainRenderNode | null,
  retrieveError: string | null,
  inspectError: string | null,
  projectionDiagnostics: MissionProjectionDiagnostics,
) {
  if (!mission) {
    return [
      "## MCP Payload Preview",
      retrieveError ? "Context request failed." : "No payload yet.",
      retrieveError || "Draft a context request to execute the live MCP context lane.",
    ];
  }

  if (tab === "Route") {
    return [
      "## Projection Path",
      `Path truth: ${mission.summary.pathTruth}`,
      `Landing nodes: ${mission.landingNodeIds.map(compactNodeId).join(", ") || "none"}`,
      `Coordinate anchors: ${projectionDiagnostics.coordinateAnchorCount}`,
      `Projection-only anchors: ${projectionDiagnostics.projectionOnlyAnchorCount}`,
      `Route nodes: ${mission.routeNodeIds.length}`,
      `Drawable route segments: ${projectionDiagnostics.drawableRouteSegmentCount}`,
      mission.routeNodeIds.length ? mission.routeNodeIds.map(compactNodeId).join(" -> ") : "No projection path.",
      "",
      "Segments:",
      ...(mission.routeSegments.length ? mission.routeSegments.map((segment) => `${compactNodeId(segment.fromNodeId)} -> ${compactNodeId(segment.toNodeId)} [${segment.source}]`) : ["No route segments."]),
    ];
  }

  if (tab === "Docs") {
    return [
      "## Document References",
      `Policy: ${mission.refsPolicy}`,
      `Anchors selected: ${mission.documentNodeIds.length}`,
      "",
      ...(mission.documentNodeIds.length ? mission.documentNodeIds.map((id) => `- ${compactNodeId(id)}`) : ["No document refs selected."]),
    ];
  }

  if (tab === "Memory") {
    const selectedLines = selectedNode
      ? [
          "",
          "## Selected Node",
          `ID: ${selectedNode.id}`,
          `Role: ${nodeMissionRole(selectedNode.id, mission) || selectedNode.visualRole}`,
          `Kind: ${selectedNode.node_kind || "unknown"}`,
          `Memory: ${selectedNode.memory_type || "unknown"}`,
          `Links: ${selectedNode.links?.length || 0}`,
          `Highways: ${selectedNode.highways?.length || 0}`,
          `Position: x ${selectedNode.position3d[0].toFixed(3)}, y ${selectedNode.position3d[1].toFixed(3)}, z ${selectedNode.position3d[2].toFixed(3)}`,
          "",
          nodeExcerpt(selectedNode),
        ]
      : ["", "Click a visible node in the brain to inspect it here."];
    return [
      "## Payload Nodes",
      `${mission.payloadNodeIds.length} payload nodes selected`,
      `Landing: ${mission.landingNodeIds.map(compactNodeId).join(", ") || "none"}`,
      "",
      ...mission.payloadNodeIds.slice(0, 12).map((id) => `- ${compactNodeId(id)} (${nodeMissionRole(id, mission)})`),
      ...selectedLines,
    ];
  }

  if (tab === "Trace") {
    return [
      "## Projection Trace",
      `Source: ${mission.source === "live_mcp" ? "live MCP backend response" : "local fixture shaped like run_projection_truth"}`,
      "Mutation: none",
      `MCP endpoint: ${mission.requestPlan.endpoint}`,
      `Delivery source: ${mission.live?.resultSource || "fixture"}`,
      `Projection raw nodes: ${projectionDiagnostics.rawNodeCount}`,
      `Projection coordinate anchors: ${projectionDiagnostics.coordinateAnchorCount}`,
      `Graph matched nodes: ${projectionDiagnostics.graphMatchedNodeCount}`,
      `Projection-only anchors: ${projectionDiagnostics.projectionOnlyAnchorCount}`,
      `Backend status: ${mission.live?.status || "not executed"}`,
      `Terminal for client: ${formatNullableBoolean(mission.live?.terminalForClient ?? null)}`,
      `Final materialization pending: ${formatNullableBoolean(mission.live?.finalMaterializationPending ?? null)}`,
      `Inspect: ${mission.live?.inspectEndpoint || "not reported"}`,
      `Query result: ${mission.live?.queryResultEndpoint || "not reported"}`,
      `Stream: ${mission.live?.streamEndpoint || "not reported"}`,
      `Created: ${mission.createdAt}`,
      `Last inspected: ${mission.live?.lastInspectedAt || "not inspected"}`,
      `Search ID: ${mission.id}`,
      inspectError ? `Inspect error: ${inspectError}` : "",
    ];
  }

  if (tab === "Validation") {
    return [
      "## Validation",
      `Payload state: ${mission.summary.payloadState}`,
      `Client state: ${mission.summary.clientCanProceed}`,
      `Route truth: ${mission.summary.pathTruth}`,
      `Documents: ${mission.summary.documents}`,
      `Run projection: ${projectionLedgerLabel(projectionDiagnostics)}`,
      `Delivery source: ${mission.live?.resultSource || mission.source}`,
      `Final pending: ${formatNullableBoolean(mission.live?.finalMaterializationPending ?? null)}`,
      `Last inspected: ${mission.live?.lastInspectedAt || "not inspected"}`,
      `Missing: ${mission.summary.missingReasons}`,
      inspectError ? `Inspect error: ${inspectError}` : "",
    ];
  }

  return mission.payloadMarkdown.split("\n");
}

function proofDockTitle(mode: CockpitModeKey, mission: MissionProjection | null) {
  if (mission) return mission.source === "live_mcp" ? "Context Payload" : "Run Projection";
  const titles: Record<CockpitModeKey, string> = {
    brain: "Brain Memory",
    retrieve: "Context Payload",
    clone_app: "Clone App Module",
    payload: "Run Results",
    chat: "Agent Chat Proof",
    paths: "Path Proof",
    documents: "Document Evidence",
    grow: "Growth Preview",
    health: "Health Proof",
    evolve: "Maintenance Preview",
    benchmarks: "Benchmark Gates",
    platform: "Platform Control Plane",
    mcp_setup: "MCP Setup Contract",
    mcp_raw_console: "MCP Raw Console",
    settings: "Settings Contract",
  };
  return titles[mode];
}

function noMissionDocumentsLabel(mode: CockpitModeKey, documentAnchorCount: number) {
  if (mode === "documents" && documentAnchorCount) return `${documentAnchorCount} graph anchors`;
  return "not requested";
}

function missionSourceLabel(mission: MissionProjection) {
  if (mission.source !== "live_mcp") return "Fixture projection";
  if (mission.live?.resultSource === "inspect_context_package") return "Live MCP inspector result";
  if (mission.status === "running") return "Context request running";
  return mission.live?.resultSource === "query_result" ? "MCP final result" : "Context package";
}

function projectionLedgerLabel(diagnostics: MissionProjectionDiagnostics) {
  if (!diagnostics.rawNodeCount && !diagnostics.coordinateAnchorCount) return "none";
  if (diagnostics.projectionOnlyAnchorCount) {
    return `${diagnostics.coordinateAnchorCount} anchors / ${diagnostics.projectionOnlyAnchorCount} coordinate-only`;
  }
  if (diagnostics.graphMatchedNodeCount) return `${diagnostics.graphMatchedNodeCount} graph nodes`;
  return `${diagnostics.rawNodeCount} raw nodes`;
}

function inspectStateLabel(mission: MissionProjection | null, running: boolean, error: string | null) {
  if (running) return "inspecting final";
  if (error) return error;
  if (!mission || mission.source !== "live_mcp") return "unavailable";
  if (mission.status === "running") return "waiting first payload";
  if (mission.live?.resultSource === "query_result") return "query-result loaded";
  if (mission.live?.resultSource === "inspect_context_package") return "inspector loaded";
  if (mission.live?.finalMaterializationPending) return "final pending";
  return "first payload only";
}

function inspectActionLabel(mission: MissionProjection | null, running: boolean) {
  if (running) return "Inspecting";
  if (!mission || mission.source !== "live_mcp") return "No Result";
  if (mission.status === "running") return "Pending";
  if (mission.live?.resultSource === "query_result" || mission.live?.resultSource === "inspect_context_package") return "Refresh final package";
  if (mission.live?.finalMaterializationPending) return "Refresh final package";
  return "Refresh package";
}

function modeProofState(mode: CockpitModeKey) {
  if (mode === "grow" || mode === "evolve") return "Preview only";
  if (mode === "chat") return "Read-only orchestration";
  if (mode === "clone_app") return "Paid module shell";
  if (mode === "health") return "Read-only health";
  if (mode === "benchmarks") return "Artifacts loaded";
  if (mode === "platform") return "Platform status";
  if (mode === "mcp_setup") return "Connector setup";
  if (mode === "mcp_raw_console") return "Raw MCP tool console";
  if (mode === "settings") return "Brain registry bound";
  return "No context run";
}

function modeProofFallback(mode: CockpitModeKey) {
  const fallbacks: Record<CockpitModeKey, string> = {
    brain: "Inspect loaded memory graph",
    retrieve: "Awaiting context command",
    clone_app: "Clone App product shell is capability-gated",
    chat: "Waiting for conversational context package",
    payload: "Waiting for delivered payload",
    paths: "Waiting for route truth",
    documents: "Waiting for document refs",
    grow: "Mutation disabled until preview contract exists",
    health: "Basic health is loaded; detailed audit not bound",
    evolve: "No maintenance preview has been requested",
    benchmarks: "Artifact-backed benchmark board",
    platform: "Central account, billing and module service is a separate Docker profile",
    mcp_setup: "MCP client setup guide, permission profile and generated config",
    mcp_raw_console: "MCP contract tool list, JSON request and raw response receipt",
    settings: "Brain registry, graph detail and safety scope are bound",
  };
  return fallbacks[mode];
}

function nodeMissionRole(id: string, mission: MissionProjection | null) {
  if (!mission) return null;
  if (mission.landingNodeIds.includes(id)) return "landing";
  if (mission.documentNodeIds.includes(id)) return "document anchor";
  if (mission.routeNodeIds.includes(id)) return "route node";
  if (mission.payloadNodeIds.includes(id)) return "payload node";
  if (mission.candidateNodeIds.includes(id)) return "candidate";
  return "context";
}

function compactNodeId(id: string) {
  return id.length > 20 ? `${id.slice(0, 9)}...${id.slice(-7)}` : id;
}

function nodeExcerpt(node: BrainRenderNode) {
  const text = node.summary || node.raw_text || node.source_unit_id || "No text preview available.";
  return text.replace(/\s+/g, " ").slice(0, 180);
}

function formatNullableBoolean(value: boolean | null) {
  if (value === true) return "true";
  if (value === false) return "false";
  return "not reported";
}

function TruthRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="truth-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
