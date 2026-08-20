// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
// SPDX-FileContributor: Lorenzo Massaro
// SPDX-License-Identifier: AGPL-3.0-only

import {
  Activity,
  ArrowRight,
  Brain,
  CheckCircle2,
  CircleAlert,
  Cloud,
  Database,
  Download,
  FileUp,
  GitBranch,
  Layers3,
  Lock,
  LucideIcon,
  MessageSquareText,
  Network,
  Play,
  PlusCircle,
  RefreshCw,
  Search,
  Server,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";

type HealthState = {
  ok?: boolean;
  service?: string;
  version?: string;
  active_brain_id?: string | null;
  brain_registry_ready?: boolean;
  runtime_scope_status?: string;
};

type BrainSummary = {
  brain_id?: string;
  id?: string;
  display_name?: string;
  name?: string;
  description?: string | null;
  is_active?: boolean;
  node_count?: number;
  safe_for_mcp?: boolean;
};

type BrainRegistry = {
  active_brain_id?: string | null;
  brain_count?: number;
  brains?: BrainSummary[];
};

type GraphNode = {
  id?: string;
  summary?: string | null;
  memory_type?: string | null;
  final_position?: { x?: number; y?: number; z?: number } | null;
  semantic_color?: { hex?: string } | null;
  links?: Array<{ target_node_id?: string; strength?: number }>;
};

type GraphResponse = {
  graph?: {
    nodes?: GraphNode[];
    meta?: {
      total_node_count?: number;
      sampled_node_count?: number;
      total_edge_count?: number;
      load_error?: string;
    };
  };
};

type ToolContract = {
  name: string;
  title?: string;
  description?: string;
  category?: string;
  endpoint_path?: string;
  http_method?: "GET" | "POST";
  permission_family?: string;
  requires_brain_id?: boolean;
  implementation_status?: string;
};

type McpRegistry = {
  registry_status?: string;
  tools?: ToolContract[];
  registry_validation?: {
    passed?: boolean;
    registered_tool_count?: number;
  };
};

type RouteId = "brain" | "context" | "grow" | "mcp" | "modules" | "health" | "settings";
type Tone = "ready" | "active" | "pending" | "blocked" | "neutral";
type BrainActivity = {
  active: boolean;
  detail: string;
  label: string;
  phase: "idle" | "retrieving" | "growing" | "mcp" | "health";
};

const apiBaseUrl = String(import.meta.env.VITE_API_URL || "http://localhost:8010").replace(/\/$/, "");
const cloudUrl = String(import.meta.env.VITE_DETWIN_CLOUD_URL || "https://app.detwin.ai").replace(/\/$/, "");

const routes: Array<{ id: RouteId; label: string; eyebrow: string; icon: LucideIcon }> = [
  { id: "brain", label: "Brain", eyebrow: "Memory shape", icon: Brain },
  { id: "context", label: "Context", eyebrow: "Retrieve", icon: Search },
  { id: "grow", label: "Grow", eyebrow: "Core write", icon: Sparkles },
  { id: "mcp", label: "MCP", eyebrow: "Raw tools", icon: TerminalSquare },
  { id: "modules", label: "Modules", eyebrow: "Core vs Cloud", icon: Layers3 },
  { id: "health", label: "Health", eyebrow: "Runtime proof", icon: Activity },
  { id: "settings", label: "Settings", eyebrow: "Local only", icon: ShieldCheck },
];

const demoNodes: GraphNode[] = Array.from({ length: 48 }, (_, index) => {
  const angle = (index / 48) * Math.PI * 2;
  const ring = index % 4;
  return {
    id: `demo-${index + 1}`,
    summary: ["Context", "Evidence", "Receipt", "Growth"][ring],
    memory_type: ["context", "evidence", "receipt", "growth"][ring],
    final_position: {
      x: Math.cos(angle) * (0.34 + ring * 0.14),
      y: Math.sin(angle) * (0.24 + ring * 0.1),
      z: Math.sin(angle * 1.7) * 0.2,
    },
    semantic_color: { hex: ["#01eab2", "#486efe", "#d0ccf0", "#ffffff"][ring] },
    links: index > 0 ? [{ target_node_id: `demo-${Math.max(1, index - ring)}`, strength: 0.72 }] : [],
  };
});

export function CockpitApp() {
  const [route, setRoute] = useState<RouteId>(() => routeFromLocation());
  const [health, setHealth] = useState<HealthState | null>(null);
  const [registry, setRegistry] = useState<BrainRegistry | null>(null);
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [mcpRegistry, setMcpRegistry] = useState<McpRegistry | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [query, setQuery] = useState("What changed in this brain recently?");
  const [sourceText, setSourceText] = useState(
    "Capture the product-readiness decision: AGVM Core stays local and free, while advanced modules run in Detwin Cloud.",
  );
  const [newBrainDisplayName, setNewBrainDisplayName] = useState("Personal Memory");
  const [newBrainId, setNewBrainId] = useState("personal_memory");
  const [importBrainDisplayName, setImportBrainDisplayName] = useState("Imported Memory");
  const [importBrainId, setImportBrainId] = useState("imported_memory");
  const [toolName, setToolName] = useState("retrieve_context");
  const [rawPayload, setRawPayload] = useState("{\n  \"query_text\": \"What should AGVM retrieve from this brain?\"\n}");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLastError(null);
    const [nextHealth, nextRegistry, nextGraph, nextMcp] = await Promise.all([
      readApi<HealthState>("/health").catch(() => null),
      readApi<BrainRegistry>("/mcp/brains").catch(() => readApi<BrainRegistry>("/memory/brains").catch(() => null)),
      readApi<GraphResponse>("/graph-view?max_nodes=900").catch(() => null),
      readApi<McpRegistry>("/mcp/contracts").catch(() => readApi<McpRegistry>("/memory/mcp/contracts").catch(() => null)),
    ]);
    setHealth(nextHealth);
    setRegistry(nextRegistry);
    setGraph(nextGraph);
    setMcpRegistry(nextMcp);
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
    const onHash = () => setRoute(routeFromLocation());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [refresh]);

  const brains = useMemo(() => registry?.brains || [], [registry]);
  const activeBrainId = String(
    registry?.active_brain_id || health?.active_brain_id || brains.find((brain) => brain.is_active)?.brain_id || brains[0]?.brain_id || "",
  );
  const activeBrain = brains.find((brain) => brainId(brain) === activeBrainId) || brains[0] || null;
  const nodes = graph?.graph?.nodes?.length ? graph.graph.nodes : demoNodes;
  const toolOptions = useMemo(() => (mcpRegistry?.tools || []).filter((tool) => tool.endpoint_path).slice(0, 80), [mcpRegistry]);
  const selectedTool = toolOptions.find((tool) => tool.name === toolName) || toolOptions[0] || null;
  const routeModel = routes.find((item) => item.id === route) || routes[0];
  const activity = activityFor(busyAction, route);

  useEffect(() => {
    if (selectedTool && selectedTool.name !== toolName) setToolName(selectedTool.name);
  }, [selectedTool, toolName]);

  async function runAction(action: string, executor: () => Promise<Record<string, unknown>>) {
    setBusyAction(action);
    setLastError(null);
    try {
      setResult(await executor());
    } catch (error: unknown) {
      setLastError(errorMessage(error));
      setResult(null);
    } finally {
      setBusyAction(null);
    }
  }

  function createLocalBrain(displayName = newBrainDisplayName, requestedBrainId = newBrainId) {
    const cleanDisplayName = displayName.trim() || "Personal Memory";
    const brain_id = normalizeBrainId(requestedBrainId || cleanDisplayName);
    runAction("create-brain", async () => {
      const response = await writeApi<Record<string, unknown>>("/memory/brains/create", {
        brain_id,
        display_name: cleanDisplayName,
        description: "Created from the AGVM Core UI.",
        make_active: true,
        make_default: true,
      });
      await refresh();
      return response;
    });
  }

  function bootstrapRegistry() {
    runAction("bootstrap-brain-registry", async () => {
      const response = await writeApi<Record<string, unknown>>("/memory/brains/bootstrap", {
        legacy_data_dirs: [],
        default_brain_id: normalizeBrainId(newBrainId || newBrainDisplayName),
        force_rescan: true,
      });
      await refresh();
      return response;
    });
  }

  function importLocalBrain(file: File | null) {
    if (!file) return;
    runAction("import-brain", async () => {
      const body = new FormData();
      body.append("archive", file);
      body.append("display_name", importBrainDisplayName.trim() || file.name.replace(/\.[^.]+$/, ""));
      body.append("brain_id", normalizeBrainId(importBrainId || file.name.replace(/\.[^.]+$/, "")));
      body.append("make_active", "true");
      body.append("make_default", "true");
      body.append("overwrite_existing", "false");
      const response = await uploadApi<Record<string, unknown>>("/memory/brains/import-upload", body);
      await refresh();
      return response;
    });
  }

  function exportActiveBrain() {
    runAction("export-brain", async () => {
      if (!activeBrainId) throw new Error("Select or create a local brain before export.");
      return writeApi<Record<string, unknown>>("/memory/brains/export", { brain_id: activeBrainId });
    });
  }

  function createDemoBrain() {
    createLocalBrain("Core Product Demo Brain", "core_product_demo_brain");
  }

  return (
    <main className="core-shell" data-route={route}>
      <aside className="core-rail" aria-label="Local AGVM navigation">
        <div className="core-brand">
          <span className="core-mark">de</span>
          <div>
            <strong>AGVM</strong>
            <small>Local Core</small>
          </div>
        </div>
        <nav>
          {routes.map((item) => (
            <a className={route === item.id ? "active" : ""} href={`#${item.id}`} key={item.id} onClick={() => setRoute(item.id)}>
              <item.icon size={17} />
              <span>{item.label}</span>
              <small>{item.eyebrow}</small>
            </a>
          ))}
        </nav>
        <div className="rail-card">
          <Server size={16} />
          <strong>{health?.ok ? "Runtime connected" : loading ? "Checking runtime" : "Runtime offline"}</strong>
          <span>{apiBaseUrl}</span>
        </div>
      </aside>

      <section className="core-main">
        <header className="core-topbar">
          <div className="topbar-title">
            <routeModel.icon size={18} />
            <div>
              <span>{routeModel.eyebrow}</span>
              <strong>{routeModel.label}</strong>
            </div>
          </div>
          <BrainSelector
            activeBrainId={activeBrainId}
            brains={brains}
            busyAction={busyAction}
            importBrainDisplayName={importBrainDisplayName}
            importBrainId={importBrainId}
            newBrainDisplayName={newBrainDisplayName}
            newBrainId={newBrainId}
            onBootstrap={bootstrapRegistry}
            onCreateBrain={() => createLocalBrain()}
            onExportBrain={exportActiveBrain}
            onImportFile={importLocalBrain}
            onRefresh={refresh}
            onSelect={(brain) =>
              runAction("select-brain", async () => {
                const response = await writeApi<Record<string, unknown>>("/memory/brains/select", { brain_id: brain, make_default: false });
                await refresh();
                return response;
              })
            }
            setImportBrainDisplayName={setImportBrainDisplayName}
            setImportBrainId={setImportBrainId}
            setNewBrainDisplayName={setNewBrainDisplayName}
            setNewBrainId={setNewBrainId}
          />
          <StatusTile label="API" value={health?.ok ? "Running" : loading ? "Checking" : "Offline"} tone={health?.ok ? "ready" : loading ? "pending" : "blocked"} />
          <StatusTile label="Graph" value={graph?.graph?.nodes?.length ? `${graph.graph.nodes.length} nodes` : "Demo shape"} tone={graph?.graph?.nodes?.length ? "active" : "pending"} />
          <button className="icon-button" onClick={refresh} title="Refresh local runtime" type="button">
            <RefreshCw size={17} />
          </button>
        </header>

        <section className="workspace">
          <div className="workspace-head">
            <div>
              <span>{routeModel.eyebrow.toUpperCase()}</span>
              <h1>{headlineForRoute(route, activeBrain)}</h1>
              <p>{descriptionForRoute(route)}</p>
            </div>
            <div className="workspace-actions">
              {!activeBrainId ? (
                <button
                  className="primary"
                  disabled={busyAction === "create-brain"}
                  onClick={createDemoBrain}
                  type="button"
                >
                  {busyAction === "create-brain" ? <RefreshCw size={16} /> : <Brain size={16} />}
                  Create demo brain
                </button>
              ) : null}
              <a className="secondary" href={`${cloudUrl}/modules`}>
                <Cloud size={16} />
                Use Detwin Cloud
              </a>
            </div>
          </div>

          {lastError ? <Notice tone="blocked" title="Local request did not complete" detail={lastError} /> : null}
          {!activeBrainId ? (
            <BrainBootstrapNotice busyAction={busyAction} onBootstrap={bootstrapRegistry} onCreateDemo={createDemoBrain} />
          ) : null}

          {route === "brain" ? <BrainRoute activeBrain={activeBrain} activeBrainId={activeBrainId} activity={activity} graph={graph} nodes={nodes} /> : null}
          {route === "context" ? (
            <ContextRoute
              activeBrainId={activeBrainId}
              activity={activity}
              busy={busyAction === "retrieve"}
              nodes={nodes}
              query={query}
              result={result}
              setQuery={setQuery}
              onRun={() =>
                runAction("retrieve", () =>
                  writeApi<Record<string, unknown>>("/memory/query", {
                    brain_id: activeBrainId || undefined,
                    query_text: query,
                    retrieval_mode: "balanced",
                    max_matches: 10,
                    include_answer_demo: true,
                    include_raw_text: false,
                  }),
                )
              }
            />
          ) : null}
          {route === "grow" ? (
            <GrowRoute
              activeBrainId={activeBrainId}
              activity={activity}
              busy={busyAction === "grow"}
              nodes={nodes}
              result={result}
              sourceText={sourceText}
              setSourceText={setSourceText}
              onRun={() =>
                runAction("grow", () =>
                  writeApi<Record<string, unknown>>("/mcp/grow-source-preview", {
                    brain_id: activeBrainId || undefined,
                    raw_input: sourceText,
                    input_kind: "manual_text",
                    source_label: "Local Core UI note",
                    run_preview: true,
                  }),
                )
              }
            />
          ) : null}
          {route === "mcp" ? (
            <McpRoute
              activeBrainId={activeBrainId}
              activity={activity}
              busy={busyAction === "mcp"}
              mcpRegistry={mcpRegistry}
              nodes={nodes}
              rawPayload={rawPayload}
              result={result}
              selectedTool={selectedTool}
              setRawPayload={setRawPayload}
              setToolName={setToolName}
              toolName={toolName}
              toolOptions={toolOptions}
              onRun={() =>
                runAction("mcp", async () => {
                  if (!selectedTool?.endpoint_path) throw new Error("Select an executable MCP tool first.");
                  const payload = parseJsonObject(rawPayload);
                  if (selectedTool.requires_brain_id && activeBrainId && selectedTool.http_method !== "GET") payload.brain_id = activeBrainId;
                  if (selectedTool.http_method === "GET") return readApi<Record<string, unknown>>(withQuery(selectedTool.endpoint_path, payload));
                  return writeApi<Record<string, unknown>>(selectedTool.endpoint_path, payload);
                })
              }
            />
          ) : null}
          {route === "modules" ? <ModulesRoute /> : null}
          {route === "health" ? (
            <HealthRoute
              activeBrainId={activeBrainId}
              activity={activity}
              busy={busyAction === "health"}
              health={health}
              nodes={nodes}
              result={result}
              onRun={() =>
                runAction("health", () =>
                  writeApi<Record<string, unknown>>("/mcp/brain-health", {
                    brain_id: activeBrainId || undefined,
                    include_issue_samples: false,
                  }),
                )
              }
            />
          ) : null}
          {route === "settings" ? <SettingsRoute activeBrainId={activeBrainId} health={health} mcpRegistry={mcpRegistry} /> : null}
        </section>
      </section>
    </main>
  );
}

function BrainRoute({
  activeBrain,
  activeBrainId,
  activity,
  graph,
  nodes,
}: {
  activeBrain: BrainSummary | null;
  activeBrainId: string;
  activity: BrainActivity;
  graph: GraphResponse | null;
  nodes: GraphNode[];
}) {
  return (
    <div className="brain-grid">
      <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} />
      <aside className="proof-panel">
        <PanelEyebrow icon={Database} label="Active memory" />
        <h2>{activeBrain ? brainName(activeBrain) : "No local brain selected"}</h2>
        <MetricGrid
          metrics={[
            { label: "Brain id", value: activeBrainId || "not selected" },
            { label: "Nodes", value: String(graph?.graph?.meta?.total_node_count || activeBrain?.node_count || nodes.length) },
            { label: "MCP", value: activeBrain?.safe_for_mcp === false ? "gated" : "ready" },
            { label: "Scope", value: "local only" },
          ]}
        />
        <div className="receipt-list">
          <Receipt title="Brain shape" detail="The canvas keeps a stable brain-like projection even before a large local graph is loaded." tone="active" />
          <Receipt title="Runtime boundary" detail="No Detwin account, billing state or cloud workspace is required for this local Core UI." tone="ready" />
          <Receipt title="Advanced modules" detail="Clone, Teach and Maintain are cloud surfaces. Local Core links to Detwin Cloud instead of downloading paid modules." tone="pending" />
        </div>
      </aside>
    </div>
  );
}

function ContextRoute({
  activeBrainId,
  activity,
  busy,
  nodes,
  onRun,
  query,
  result,
  setQuery,
}: {
  activeBrainId: string;
  activity: BrainActivity;
  busy: boolean;
  nodes: GraphNode[];
  onRun: () => void;
  query: string;
  result: Record<string, unknown> | null;
  setQuery: (value: string) => void;
}) {
  return (
    <div className="operation-grid">
      <section className="command-surface">
        <PanelEyebrow icon={Search} label="Retrieve Context" />
        <textarea onChange={(event) => setQuery(event.target.value)} value={query} />
        <button className="primary wide" disabled={busy || !query.trim()} onClick={onRun} type="button">
          {busy ? <RefreshCw size={17} /> : <Play size={17} />}
          Run local retrieval
        </button>
        <p className="fine-print">Runs against `/memory/query` on the local AGVM API. Local retrieval does not consume Detwin credits.</p>
      </section>
      <div className="live-result-stack">
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
        <ResultPanel emptyTitle={activeBrainId ? "Awaiting retrieval" : "Select or create a brain first"} result={result} />
      </div>
    </div>
  );
}

function GrowRoute({
  activeBrainId,
  activity,
  busy,
  nodes,
  onRun,
  result,
  setSourceText,
  sourceText,
}: {
  activeBrainId: string;
  activity: BrainActivity;
  busy: boolean;
  nodes: GraphNode[];
  onRun: () => void;
  result: Record<string, unknown> | null;
  setSourceText: (value: string) => void;
  sourceText: string;
}) {
  return (
    <div className="operation-grid">
      <section className="command-surface">
        <PanelEyebrow icon={Sparkles} label="Grow Source Preview" />
        <textarea onChange={(event) => setSourceText(event.target.value)} value={sourceText} />
        <button className="primary wide" disabled={busy || !sourceText.trim()} onClick={onRun} type="button">
          {busy ? <RefreshCw size={17} /> : <GitBranch size={17} />}
          Preview memory growth
        </button>
        <p className="fine-print">Preview is local and explicit. Apply/write actions stay behind MCP confirmation contracts.</p>
      </section>
      <div className="live-result-stack">
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
        <ResultPanel emptyTitle={activeBrainId ? "Grow preview has not run" : "Create a brain before growing memory"} result={result} />
      </div>
    </div>
  );
}

function McpRoute({
  activeBrainId,
  activity,
  busy,
  mcpRegistry,
  nodes,
  onRun,
  rawPayload,
  result,
  selectedTool,
  setRawPayload,
  setToolName,
  toolName,
  toolOptions,
}: {
  activeBrainId: string;
  activity: BrainActivity;
  busy: boolean;
  mcpRegistry: McpRegistry | null;
  nodes: GraphNode[];
  onRun: () => void;
  rawPayload: string;
  result: Record<string, unknown> | null;
  selectedTool: ToolContract | null;
  setRawPayload: (value: string) => void;
  setToolName: (value: string) => void;
  toolName: string;
  toolOptions: ToolContract[];
}) {
  return (
    <div className="operation-grid">
      <section className="command-surface">
        <PanelEyebrow icon={TerminalSquare} label="Raw MCP console" />
        <div className="field-grid">
          <label>
            Tool
            <select onChange={(event) => setToolName(event.target.value)} value={toolName}>
              {toolOptions.length ? toolOptions.map((tool) => <option key={tool.name} value={tool.name}>{tool.name}</option>) : <option value="">No executable catalog loaded</option>}
            </select>
          </label>
          <label>
            Contract
            <input readOnly value={selectedTool?.endpoint_path || "catalog unavailable"} />
          </label>
        </div>
        <textarea className="json-editor" onChange={(event) => setRawPayload(event.target.value)} value={rawPayload} />
        <button className="primary wide" disabled={busy || !selectedTool} onClick={onRun} type="button">
          {busy ? <RefreshCw size={17} /> : <TerminalSquare size={17} />}
          Invoke local MCP tool
        </button>
        <p className="fine-print">
          Catalog status: {mcpRegistry?.registry_status || "not loaded"}.
          {activeBrainId ? " Active brain is injected when required." : " Select a brain for brain-scoped tools."}
        </p>
      </section>
      <div className="live-result-stack">
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
        <ResultPanel emptyTitle="MCP output will appear here" result={result} />
      </div>
    </div>
  );
}

function ModulesRoute() {
  const lockedTools = ["Clone Chat", "Clone Teach", "Maintain Sleep", "Maintain Evolve", "Maintain Matrix"];
  return (
    <div className="module-grid">
      <article className="module-card included">
        <span className="module-badge">Core included</span>
        <PanelEyebrow icon={Sparkles} label="Local Grow" />
        <h2>Grow runs in AGVM Core.</h2>
        <p>Use local MCP preview/write tools against your selected brain. No cloud account or Detwin credits are required for local Core execution.</p>
        <a className="primary link-button" href="#grow"><ArrowRight size={16} /> Open Grow</a>
      </article>
      {lockedTools.map((tool) => (
        <article className="module-card cloud" key={tool}>
          <span className="module-badge pro">Use Detwin Cloud</span>
          <PanelEyebrow icon={Lock} label={tool} />
          <h2>{tool} stays cloud-only.</h2>
          <p>Advanced module execution is served by Detwin Cloud with account, plan, provider, brain and credit checks before it runs.</p>
          <a className="secondary link-button" href={`${cloudUrl}/modules`}><Cloud size={16} /> Open cloud modules</a>
        </article>
      ))}
    </div>
  );
}

function HealthRoute({
  activeBrainId,
  activity,
  busy,
  health,
  nodes,
  onRun,
  result,
}: {
  activeBrainId: string;
  activity: BrainActivity;
  busy: boolean;
  health: HealthState | null;
  nodes: GraphNode[];
  onRun: () => void;
  result: Record<string, unknown> | null;
}) {
  return (
    <div className="operation-grid">
      <section className="command-surface">
        <PanelEyebrow icon={Activity} label="Runtime Health" />
        <MetricGrid
          metrics={[
            { label: "API", value: health?.ok ? "running" : "offline" },
            { label: "Service", value: health?.service || "AGVM Core" },
            { label: "Version", value: health?.version || "not reported" },
            { label: "Active brain", value: activeBrainId || "not selected" },
          ]}
        />
        <button className="primary wide" disabled={busy} onClick={onRun} type="button">
          {busy ? <RefreshCw size={17} /> : <Activity size={17} />}
          Run brain health proof
        </button>
      </section>
      <div className="live-result-stack">
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
        <ResultPanel emptyTitle="Health proof has not run" result={result} />
      </div>
    </div>
  );
}

function SettingsRoute({ activeBrainId, health, mcpRegistry }: { activeBrainId: string; health: HealthState | null; mcpRegistry: McpRegistry | null }) {
  return (
    <div className="settings-grid">
      <Notice tone="ready" title="Local-first boundary" detail="This UI talks only to the local AGVM API configured with VITE_API_URL. It does not sign in, sync, bill, or unlock cloud modules." />
      <MetricGrid
        metrics={[
          { label: "API base", value: apiBaseUrl },
          { label: "Cloud link", value: cloudUrl },
          { label: "Active brain", value: activeBrainId || "not selected" },
          { label: "MCP tools", value: String(mcpRegistry?.tools?.length || 0) },
          { label: "Registry", value: health?.brain_registry_ready ? "ready" : "not ready" },
          { label: "Credits", value: "not used locally" },
        ]}
      />
    </div>
  );
}

function BrainCanvas({
  activeBrainId,
  activity,
  nodes,
  variant = "stage",
}: {
  activeBrainId: string;
  activity: BrainActivity;
  nodes: GraphNode[];
  variant?: "stage" | "compact";
}) {
  const visibleNodes = nodes.slice(0, 90);
  const points = visibleNodes.map((node, index) => nodePoint(node, index));
  const pathIndexes = points
    .map((point, index) => ({ index, point }))
    .filter((_, index) => index % 5 === 0 || index % 7 === 0)
    .slice(0, 11);
  const activePathIndexSet = new Set(pathIndexes.map((item) => item.index));
  const pathPoints = pathIndexes.map((item) => `${item.point.x.toFixed(2)},${item.point.y.toFixed(2)}`).join(" ");
  return (
    <section className={`brain-canvas ${variant} ${activity.active ? "is-active" : "is-idle"}`} aria-label="Local AGVM brain projection">
      <div className="brain-orbit" />
      <svg aria-hidden="true" className="brain-paths" preserveAspectRatio="none" viewBox="0 0 100 100">
        <polyline className="brain-path ghost" points={pathPoints} />
        <polyline className="brain-path live" points={pathPoints} />
        {pathIndexes.map((item, index) => (
          <circle className="brain-path-stop" cx={item.point.x} cy={item.point.y} key={`${item.index}-${index}`} r={activity.active ? 1.2 : 0.7} />
        ))}
      </svg>
      <div className="brain-core">
        {visibleNodes.map((node, index) => {
          const point = points[index];
          const style = {
            "--x": `${point.x}%`,
            "--y": `${point.y}%`,
            "--size": `${5 + (index % 5)}px`,
            "--delay": `${(index % 13) * 0.21}s`,
            "--node-color": node.semantic_color?.hex || (index % 3 === 0 ? "#01eab2" : index % 3 === 1 ? "#486efe" : "#d0ccf0"),
          } as CSSProperties;
          return (
            <span
              className={`brain-node ${activePathIndexSet.has(index) ? "on-path" : ""}`}
              key={`${node.id || "node"}-${index}`}
              style={style}
              title={node.summary || node.id || "memory node"}
            />
          );
        })}
      </div>
      <div className="brain-hud top-left">
        <span>Active brain</span>
        <strong>{activeBrainId || "demo projection"}</strong>
      </div>
      <div className="brain-hud bottom-right">
        <span>{activity.label}</span>
        <strong>{activity.detail}</strong>
      </div>
    </section>
  );
}

type BrainSelectorProps = {
  activeBrainId: string;
  brains: BrainSummary[];
  busyAction: string | null;
  importBrainDisplayName: string;
  importBrainId: string;
  newBrainDisplayName: string;
  newBrainId: string;
  onBootstrap: () => void;
  onCreateBrain: () => void;
  onExportBrain: () => void;
  onImportFile: (file: File | null) => void;
  onRefresh: () => void;
  onSelect: (brainId: string) => void;
  setImportBrainDisplayName: (value: string) => void;
  setImportBrainId: (value: string) => void;
  setNewBrainDisplayName: (value: string) => void;
  setNewBrainId: (value: string) => void;
};

function BrainSelector({
  activeBrainId,
  brains,
  busyAction,
  importBrainDisplayName,
  importBrainId,
  newBrainDisplayName,
  newBrainId,
  onBootstrap,
  onCreateBrain,
  onExportBrain,
  onImportFile,
  onRefresh,
  onSelect,
  setImportBrainDisplayName,
  setImportBrainId,
  setNewBrainDisplayName,
  setNewBrainId,
}: BrainSelectorProps) {
  const busy = Boolean(busyAction);
  return (
    <section className="brain-selector brain-management" title="Active local brain">
      <label>
        <Brain size={15} />
        <span>Active brain</span>
        <select disabled={!brains.length || busyAction === "select-brain"} onChange={(event) => onSelect(event.target.value)} value={activeBrainId || brains[0]?.brain_id || ""}>
          {brains.length ? brains.map((brain) => <option key={brainId(brain)} value={brainId(brain)}>{brainName(brain)}</option>) : <option value="">No local brain</option>}
        </select>
      </label>
      <details className="brain-actions-menu">
        <summary>Manage</summary>
        <div className="brain-menu-panel">
          <fieldset>
            <legend>Create local brain</legend>
            <input aria-label="New brain display name" onChange={(event) => setNewBrainDisplayName(event.target.value)} value={newBrainDisplayName} />
            <input aria-label="New brain id" onChange={(event) => setNewBrainId(event.target.value)} value={newBrainId} />
            <button disabled={busy} onClick={onCreateBrain} type="button"><PlusCircle size={15} />Create and select</button>
          </fieldset>
          <fieldset>
            <legend>Import brain archive</legend>
            <input aria-label="Imported brain display name" onChange={(event) => setImportBrainDisplayName(event.target.value)} value={importBrainDisplayName} />
            <input aria-label="Imported brain id" onChange={(event) => setImportBrainId(event.target.value)} value={importBrainId} />
            <label className="file-action">
              <FileUp size={15} />
              Import .zip
              <input accept=".zip,.agvm-brain,.agvm-brain.zip" disabled={busy} onChange={(event) => onImportFile(event.currentTarget.files?.[0] || null)} type="file" />
            </label>
          </fieldset>
          <div className="brain-menu-actions">
            <button disabled={busy} onClick={onBootstrap} type="button"><RefreshCw size={15} />Bootstrap registry</button>
            <button disabled={busy || !activeBrainId} onClick={onExportBrain} type="button"><Download size={15} />Export active</button>
            <button disabled={busy} onClick={onRefresh} type="button"><RefreshCw size={15} />Refresh</button>
            <a href="#brain"><Brain size={15} />Open Brain Center</a>
          </div>
        </div>
      </details>
    </section>
  );
}

function BrainBootstrapNotice({ busyAction, onBootstrap, onCreateDemo }: { busyAction: string | null; onBootstrap: () => void; onCreateDemo: () => void }) {
  const busy = Boolean(busyAction);
  return (
    <article className="bootstrap-notice">
      <Brain size={22} />
      <div>
        <strong>Create or import a local brain to start.</strong>
        <p>Context, Grow and MCP are brain-scoped. Bootstrap scans the local registry; Create demo brain makes a synthetic Core brain you can use immediately.</p>
      </div>
      <button className="primary" disabled={busy} onClick={onCreateDemo} type="button"><PlusCircle size={16} />Create demo brain</button>
      <button className="secondary" disabled={busy} onClick={onBootstrap} type="button"><RefreshCw size={16} />Bootstrap registry</button>
    </article>
  );
}

function StatusTile({ label, tone, value }: { label: string; tone: Tone; value: string }) {
  return (
    <article className={`status-tile ${tone}`}>
      <i />
      <div>
        <strong>{value}</strong>
        <span>{label}</span>
      </div>
    </article>
  );
}

function Notice({ detail, title, tone }: { detail: string; title: string; tone: "ready" | "blocked" | "pending" }) {
  const Icon = tone === "ready" ? CheckCircle2 : tone === "blocked" ? CircleAlert : RefreshCw;
  return (
    <article className={`notice ${tone}`}>
      <Icon size={18} />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </article>
  );
}

function PanelEyebrow({ icon: Icon, label }: { icon: LucideIcon; label: string }) {
  return (
    <span className="panel-eyebrow">
      <Icon size={14} />
      {label}
    </span>
  );
}

function MetricGrid({ metrics }: { metrics: Array<{ label: string; value: string }> }) {
  return (
    <div className="metric-grid">
      {metrics.map((metric) => (
        <article key={metric.label}>
          <span>{metric.label}</span>
          <strong>{metric.value}</strong>
        </article>
      ))}
    </div>
  );
}

function Receipt({ detail, title, tone }: { detail: string; title: string; tone: Tone }) {
  return (
    <article className={`receipt ${tone}`}>
      <i />
      <div>
        <strong>{title}</strong>
        <span>{detail}</span>
      </div>
    </article>
  );
}

function ResultPanel({ emptyTitle, result }: { emptyTitle: string; result: Record<string, unknown> | null }) {
  return (
    <section className="result-panel">
      <PanelEyebrow icon={MessageSquareText} label="Receipt" />
      {result ? (
        <pre>{JSON.stringify(result, null, 2)}</pre>
      ) : (
        <div className="empty-result">
          <Network size={26} />
          <strong>{emptyTitle}</strong>
          <span>The next successful local call will render the exact JSON receipt here.</span>
        </div>
      )}
    </section>
  );
}

async function readApi<T>(path: string): Promise<T> {
  return requestApi<T>(path, { method: "GET" });
}

async function writeApi<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return requestApi<T>(path, { method: "POST", body: JSON.stringify(compact(body)) });
}

async function uploadApi<T>(path: string, body: FormData): Promise<T> {
  return requestApi<T>(path, { method: "POST", body }, false);
}

async function requestApi<T>(path: string, init: RequestInit, jsonBody = true): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(`${apiBaseUrl}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(jsonBody ? { "Content-Type": "application/json" } : {}),
        ...(init.headers || {}),
      },
      signal: controller.signal,
    });
    const text = await response.text();
    const payload: unknown = text ? JSON.parse(text) : {};
    if (!response.ok) throw new Error(responseDetail(payload) || `API returned ${response.status}`);
    return payload as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

function routeFromLocation(): RouteId {
  const raw = window.location.hash.replace(/^#/, "") || new URLSearchParams(window.location.search).get("route") || "brain";
  return routes.some((item) => item.id === raw) ? (raw as RouteId) : "brain";
}

function activityFor(busyAction: string | null, route: RouteId): BrainActivity {
  if (busyAction === "retrieve") {
    return { active: true, detail: "path corridor resolving", label: "Retrieval running", phase: "retrieving" };
  }
  if (busyAction === "grow") {
    return { active: true, detail: "source preview routing", label: "Grow preview", phase: "growing" };
  }
  if (busyAction === "mcp") {
    return { active: true, detail: "tool contract executing", label: "MCP call", phase: "mcp" };
  }
  if (busyAction === "health") {
    return { active: true, detail: "health proof scanning", label: "Brain health", phase: "health" };
  }
  if (route === "context") return { active: false, detail: "ready for retrieval", label: "Context path", phase: "idle" };
  if (route === "grow") return { active: false, detail: "preview required", label: "Growth path", phase: "idle" };
  if (route === "mcp") return { active: false, detail: "raw catalog ready", label: "MCP path", phase: "idle" };
  if (route === "health") return { active: false, detail: "proof idle", label: "Health path", phase: "idle" };
  return { active: false, detail: "radial memory map", label: "Shape lock", phase: "idle" };
}

function headlineForRoute(route: RouteId, activeBrain: BrainSummary | null) {
  if (route === "brain") return activeBrain ? brainName(activeBrain) : "Shape a local brain before you run.";
  if (route === "context") return "Retrieve from local memory.";
  if (route === "grow") return "Grow remains local and explicit.";
  if (route === "mcp") return "Inspect and invoke raw Core MCP tools.";
  if (route === "modules") return "Core here. Advanced modules in Cloud.";
  if (route === "health") return "Prove the runtime before changing memory.";
  return "Local settings stay on this machine.";
}

function descriptionForRoute(route: RouteId) {
  if (route === "brain") return "The brain selector, runtime status and animated projection stay visible while you move across Core routes.";
  if (route === "context") return "Ask the selected local brain for a context package and inspect the receipt returned by the local API.";
  if (route === "grow") return "Preview source growth through the local MCP contract. Nothing is applied without an explicit tool call.";
  if (route === "mcp") return "Load the local MCP catalog, select a contract and execute it directly against the Core server.";
  if (route === "modules") return "Grow is included in AGVM Core. Clone, Teach, Maintain and automation are Cloud-only surfaces.";
  if (route === "health") return "Run health proof against the selected brain and keep the result separate from cloud readiness.";
  return "Configure local API and Cloud handoff links without storing account, billing or provider state here.";
}

function brainId(brain: BrainSummary) {
  return String(brain.brain_id || brain.id || "");
}

function brainName(brain: BrainSummary) {
  return String(brain.display_name || brain.name || brain.brain_id || brain.id || "Unnamed brain");
}

function normalizeBrainId(value: string) {
  const normalized = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 120);
  return normalized || "local_brain";
}

function nodePoint(node: GraphNode, index: number) {
  const position = node.final_position || {};
  const syntheticAngle = (index / 90) * Math.PI * 2;
  const x = clamp(50 + Number(position.x || Math.cos(syntheticAngle) * 0.62) * 34, 8, 92);
  const y = clamp(50 + Number(position.y || Math.sin(syntheticAngle * 1.18) * 0.46) * 42, 8, 92);
  return { x, y };
}

function parseJsonObject(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value || "{}");
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Payload must be a JSON object.");
  return parsed as Record<string, unknown>;
}

function withQuery(path: string, payload: Record<string, unknown>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(payload)) {
    if (value === undefined || value === null || value === "") continue;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

function compact(value: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== undefined && item !== null && item !== ""));
}

function responseDetail(payload: unknown) {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }
  return "";
}

function errorMessage(error: unknown) {
  if (error instanceof Error) return error.name === "AbortError" ? "The local API request timed out." : error.message;
  return "Unknown local API error.";
}

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}
