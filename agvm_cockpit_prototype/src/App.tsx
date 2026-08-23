// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
// SPDX-FileContributor: Lorenzo Massaro
// SPDX-License-Identifier: AGPL-3.0-only

import {
  Activity,
  ArrowRight,
  BarChart3,
  Blocks,
  Brain,
  CheckCircle2,
  CircleAlert,
  Cloud,
  CloudUpload,
  ClipboardCheck,
  Database,
  Download,
  FileUp,
  GitBranch,
  Globe2,
  HeartPulse,
  Layers3,
  Link2,
  Lock,
  LucideIcon,
  MessageSquareText,
  Moon,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  Play,
  Plug,
  PlusCircle,
  RefreshCw,
  Search,
  Server,
  Settings,
  ShieldCheck,
  Sparkles,
  Sun,
  TerminalSquare,
  UploadCloud,
} from "lucide-react";
import { OrbitControls } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Quaternion, Vector3, type Group } from "three";

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

type RouteId =
  | "context"
  | "results"
  | "brain_explorer"
  | "health"
  | "bench"
  | "modules"
  | "grow"
  | "maintain"
  | "mcp"
  | "brain_sync"
  | "settings";
type Tone = "ready" | "active" | "pending" | "blocked" | "neutral";
type ThemeMode = "light" | "dark";
type GrowSourceKind = "manual_text" | "url" | "website" | "pdf" | "docx" | "transcript" | "mixed_bundle";
type BrainActivity = {
  active: boolean;
  detail: string;
  label: string;
  phase: "idle" | "retrieving" | "growing" | "mcp" | "health";
};

type BrainPoint3d = {
  id: string;
  label: string;
  memoryType: string;
  position: [number, number, number];
  color: string;
  size: number;
};

const apiBaseUrl = String(import.meta.env.VITE_API_URL || "http://localhost:8010").replace(/\/$/, "");
const cloudUrl = String(import.meta.env.VITE_DETWIN_CLOUD_URL || "https://app.detwin.ai").replace(/\/$/, "");

const routes: Array<{ id: RouteId; label: string; shortLabel?: string; eyebrow: string; icon: LucideIcon }> = [
  { id: "context", label: "Context", eyebrow: "Core", icon: Search },
  { id: "results", label: "Results", eyebrow: "Core", icon: BarChart3 },
  { id: "brain_explorer", label: "Brain Explorer", shortLabel: "Explorer", eyebrow: "Core", icon: Brain },
  { id: "health", label: "Health", eyebrow: "Core", icon: HeartPulse },
  { id: "bench", label: "Bench", eyebrow: "Core", icon: Activity },
  { id: "modules", label: "Modules", eyebrow: "Core + Cloud", icon: Blocks },
  { id: "grow", label: "Grow", eyebrow: "Core", icon: Database },
  { id: "maintain", label: "Maintain", eyebrow: "Cloud-backed", icon: Activity },
  { id: "mcp", label: "MCP", eyebrow: "Connect", icon: Plug },
  { id: "brain_sync", label: "Brain Sync", shortLabel: "Sync", eyebrow: "Explicit", icon: CloudUpload },
  { id: "settings", label: "Settings", eyebrow: "Local", icon: Settings },
];

const growSourceModes: Array<{ kind: GrowSourceKind; label: string; meta: string; icon: LucideIcon }> = [
  { kind: "manual_text", label: "Text", meta: "Paste notes", icon: ClipboardCheck },
  { kind: "url", label: "URL", meta: "Single source", icon: Link2 },
  { kind: "website", label: "Website", meta: "Crawl handoff", icon: Globe2 },
  { kind: "pdf", label: "PDF", meta: "Upload or extract", icon: FileUp },
  { kind: "docx", label: "DOCX", meta: "Document", icon: FileUp },
  { kind: "transcript", label: "Transcript", meta: "Interview / OCR", icon: MessageSquareText },
  { kind: "mixed_bundle", label: "Bundle", meta: "Mixed evidence", icon: Layers3 },
];

export function CockpitApp() {
  const [route, setRoute] = useState<RouteId>(() => routeFromLocation());
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      const stored = window.localStorage?.getItem("agvm.sidebar.collapsed");
      return stored === null ? window.innerWidth <= 1100 : stored === "true";
    } catch {
      return false;
    }
  });
  const [health, setHealth] = useState<HealthState | null>(null);
  const [registry, setRegistry] = useState<BrainRegistry | null>(null);
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [mcpRegistry, setMcpRegistry] = useState<McpRegistry | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [query, setQuery] = useState("What changed in this brain recently?");
  const [theme, setTheme] = useState<ThemeMode>(() => readTheme());
  const [sourceKind, setSourceKind] = useState<GrowSourceKind>("manual_text");
  const [sourceLabel, setSourceLabel] = useState("Local Core source");
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceFileName, setSourceFileName] = useState("");
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

  useEffect(() => {
    document.documentElement.dataset.coreTheme = theme;
    document.documentElement.dataset.agvmTheme = theme;
    window.localStorage?.setItem("agvm.core.theme", theme);
  }, [theme]);

  const brains = useMemo(() => registry?.brains || [], [registry]);
  const activeBrainId = String(
    registry?.active_brain_id || health?.active_brain_id || brains.find((brain) => brain.is_active)?.brain_id || brains[0]?.brain_id || "",
  );
  const activeBrain = brains.find((brain) => brainId(brain) === activeBrainId) || brains[0] || null;
  const nodes = graph?.graph?.nodes || [];
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
      const payload = {
        brain_id,
        display_name: cleanDisplayName,
        description: "Created from the AGVM Core UI.",
        make_active: true,
        make_default: true,
      };
      const response = await writeApiWithFallback<Record<string, unknown>>("/mcp/brains/create", "/memory/brains/create", payload);
      await refresh();
      return response;
    });
  }

  function bootstrapRegistry() {
    runAction("bootstrap-brain-registry", async () => {
      const brain_id = normalizeBrainId(newBrainId || newBrainDisplayName);
      const response = await writeApiWithFallback<Record<string, unknown>>(
        "/mcp/brains/ensure",
        "/memory/brains/bootstrap",
        {
          brain_id,
          default_brain_id: brain_id,
          display_name: newBrainDisplayName.trim() || "Personal Memory",
          description: "Bootstrapped from the AGVM Core UI.",
          purpose: "local_core",
          activation_policy: "make_default",
          create_if_missing: true,
          force_rescan: true,
          legacy_data_dirs: [],
        },
      );
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
      return writeApiWithFallback<Record<string, unknown>>("/mcp/brains/export", "/memory/brains/export", { brain_id: activeBrainId });
    });
  }

  function createDemoBrain() {
    createLocalBrain("Core Product Demo Brain", "core_product_demo_brain");
  }

  async function loadGrowSourceFile(file: File | null) {
    if (!file) return;
    setSourceFileName(`${file.name} / ${formatBytes(file.size)}`);
    setSourceLabel(file.name);
    setSourceKind(inputKindForFile(file));
    if (/^text\/|json|markdown|csv|xml|yaml/i.test(file.type) || /\.(txt|md|markdown|json|csv|xml|yaml|yml)$/i.test(file.name)) {
      setSourceText(await file.text());
      return;
    }
    setSourceText(
      [
        `Uploaded source file: ${file.name}`,
        `File type: ${file.type || "unknown"}`,
        `Size: ${formatBytes(file.size)}`,
        "For binary PDF/DOCX/image sources, run the same Grow source preview contract through Cloud AGVM or provide extracted text here for Local Core preview.",
      ].join("\n"),
    );
  }

  return (
    <section
      className={`agvm-product-shell agvm-product-shell-local ${sidebarCollapsed ? "sidebar-collapsed" : "sidebar-expanded"}`}
      data-active-route={route}
      data-runtime-mode="local"
    >
      <header className="agvm-product-topbar" aria-label="Local AGVM status">
        <div className="agvm-product-brand">
          <span className="agvm-product-mark"><Brain size={20} /></span>
          <div><strong>AGVM</strong><span>Local AGVM</span></div>
        </div>
        <div className="agvm-product-context-strip" aria-label="Workspace context">
          <StatusTile label="Workspace" value="Local Workspace" tone="neutral" />
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
                const response = await writeApiWithFallback<Record<string, unknown>>("/mcp/select-brain", "/memory/brains/select", { brain_id: brain, make_default: false });
                await refresh();
                return response;
              })
            }
            setImportBrainDisplayName={setImportBrainDisplayName}
            setImportBrainId={setImportBrainId}
            setNewBrainDisplayName={setNewBrainDisplayName}
            setNewBrainId={setNewBrainId}
          />
          <StatusTile label="Plan" value="Local Core" tone="neutral" />
          <StatusTile label="Brain detail" value={nodes.length ? `${nodes.length} nodes` : "0 / 0 nodes"} tone={nodes.length ? "active" : "pending"} />
        </div>
        <div className="agvm-product-session">
          <i className={`runtime-dot ${health?.ok ? "ready" : loading ? "pending" : "blocked"}`} />
          <div>
            <strong>{health?.ok ? "Verified local access" : loading ? "Checking local runtime" : "Local runtime offline"}</strong>
            <span>{health?.service || "Docker backend"}</span>
          </div>
          <button className="icon-button" onClick={refresh} title="Refresh local runtime" type="button"><RefreshCw size={17} /></button>
        </div>
      </header>

      <div className="agvm-product-layout without-module-rail">
        <aside className="agvm-product-sidebar" aria-label="AGVM navigation">
          <button
            aria-label={sidebarCollapsed ? "Expand navigation" : "Collapse navigation"}
            className="agvm-sidebar-toggle"
            onClick={() => setSidebarCollapsed((current) => {
              const next = !current;
              window.localStorage?.setItem("agvm.sidebar.collapsed", String(next));
              return next;
            })}
            title={sidebarCollapsed ? "Expand navigation" : "Collapse navigation"}
            type="button"
          >
            {sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
            <span>{sidebarCollapsed ? "Expand" : "Collapse"}</span>
          </button>
          <nav>
            {routes.map((item) => (
              <button
                aria-current={route === item.id ? "page" : undefined}
                aria-label={item.label}
                className={`agvm-product-nav-item ${route === item.id ? "active" : ""}`}
                data-tooltip={item.label}
                key={item.id}
                onClick={() => {
                  setRoute(item.id);
                  window.location.hash = item.id;
                }}
                title={item.label}
                type="button"
              >
                <item.icon size={17} />
                <span className="agvm-product-nav-copy"><strong>{item.shortLabel || item.label}</strong><em>{item.eyebrow}</em></span>
              </button>
            ))}
          </nav>
          <div className="agvm-product-sidebar-card">
            <span>Mode</span><strong>Local</strong>
            <p>Local runtime keeps control on this device.</p>
          </div>
        </aside>

        <main className="agvm-product-main">
          <section className="agvm-product-content">
          {route !== "context" && route !== "brain_explorer" ? (
            <header className="workspace-head agvm-product-page-header">
              <div><span>{routeModel.eyebrow.toUpperCase()}</span><h1>{headlineForRoute(route, activeBrain)}</h1><p>{descriptionForRoute(route)}</p></div>
              <div className="workspace-actions">
                {!activeBrainId ? <button className="primary" disabled={busyAction === "create-brain"} onClick={createDemoBrain} type="button"><Brain size={16} />Create starter brain</button> : null}
                <a className="secondary" href={`${cloudUrl}/modules`}><Cloud size={16} />Use Detwin Cloud</a>
              </div>
            </header>
          ) : null}
          {lastError ? <Notice tone="blocked" title="Local request did not complete" detail={lastError} /> : null}
          {!activeBrainId && route !== "context" ? <BrainBootstrapNotice busyAction={busyAction} onBootstrap={bootstrapRegistry} onCreateDemo={createDemoBrain} /> : null}

          {route === "brain_explorer" ? <BrainRoute activeBrain={activeBrain} activeBrainId={activeBrainId} activity={activity} graph={graph} nodes={nodes} /> : null}
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
          {route === "results" ? <ResultsRoute nodes={nodes} result={result} /> : null}
          {route === "grow" ? (
            <GrowRoute
              activeBrainId={activeBrainId}
              activity={activity}
              busy={busyAction === "grow"}
              nodes={nodes}
              sourceFileName={sourceFileName}
              sourceKind={sourceKind}
              sourceLabel={sourceLabel}
              result={result}
              setSourceKind={setSourceKind}
              setSourceLabel={setSourceLabel}
              sourceText={sourceText}
              sourceUrl={sourceUrl}
              setSourceUrl={setSourceUrl}
              setSourceText={setSourceText}
              onFileChange={(file) => void loadGrowSourceFile(file)}
              onRun={() =>
                runAction("grow", () =>
                  writeApi<Record<string, unknown>>("/mcp/grow-source-preview", {
                    brain_id: activeBrainId || undefined,
                    raw_input: growRawInput(sourceKind, sourceText, sourceUrl, sourceFileName),
                    input_kind: sourceKind,
                    source_label: sourceLabel,
                    source_uri: sourceKind === "url" || sourceKind === "website" ? sourceUrl : undefined,
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
          {route === "maintain" ? <CloudMaintainRoute /> : null}
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
          {route === "bench" ? <BenchRoute activeBrainId={activeBrainId} graph={graph} health={health} /> : null}
          {route === "brain_sync" ? <BrainSyncRoute activeBrain={activeBrain} activeBrainId={activeBrainId} /> : null}
          {route === "settings" ? <SettingsRoute activeBrainId={activeBrainId} health={health} mcpRegistry={mcpRegistry} setTheme={setTheme} theme={theme} /> : null}
          </section>
        </main>
      </div>
    </section>
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
    <section className="brain-explorer-workspace">
      <header className="brain-explorer-header">
        <div><span>Brain Core</span><h2>Brain Explorer</h2><p>Inspect real memory nodes and their current position in the active local brain.</p></div>
        <strong>{nodes.length ? `${nodes.length} loaded nodes` : "Waiting for local graph"}</strong>
      </header>
      <div className="brain-explorer-layout">
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} />
        <aside className="proof-panel brain-explorer-inspector">
        <PanelEyebrow icon={Database} label="Active memory" />
        <h2>{activeBrain ? brainName(activeBrain) : "No local brain selected"}</h2>
        <MetricGrid
          metrics={[
            { label: "Brain id", value: activeBrainId || "not selected" },
            { label: "Nodes", value: String(graph?.graph?.meta?.total_node_count || activeBrain?.node_count || 0) },
            { label: "MCP", value: !activeBrain ? "select brain" : activeBrain.safe_for_mcp === false ? "gated" : "ready" },
            { label: "Scope", value: "local only" },
          ]}
        />
        <div className="receipt-list">
          <Receipt title="Brain shape" detail="The 3D projection is derived only from the nodes currently stored in this brain." tone="active" />
          <Receipt title="Runtime boundary" detail="No Detwin account, billing state or cloud workspace is required for this local Core UI." tone="ready" />
          <Receipt title="Advanced modules" detail="Paid workflows remain cloud surfaces. Local Core links to Detwin Cloud instead of downloading their source." tone="pending" />
        </div>
        </aside>
      </div>
    </section>
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
    <section className="context-core-workspace">
      <div className="context-command-bar">
        <Search size={17} />
        <input aria-label="Context query" onChange={(event) => setQuery(event.target.value)} placeholder="Ask the brain for context..." value={query} />
        <button className="secondary" disabled={!activeBrainId} type="button">
          <Brain size={16} />{activeBrainId ? "Active brain" : "Choose brain"}
        </button>
        <button className="primary" disabled={busy || !activeBrainId || !query.trim()} onClick={onRun} type="button">
          {busy ? <RefreshCw size={17} /> : <Play size={17} />}
          {activeBrainId ? "Run retrieval" : "Create, import or select a brain"}
        </button>
      </div>
      <div className="context-live-layout">
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
        <aside className="context-insight-rail">
          <PanelEyebrow icon={Search} label="Search status" />
          <strong>{busy ? "Retrieval active" : result ? "Result ready" : "Awaiting mission"}</strong>
          <div className="context-progress" aria-label="Context progress"><i style={{ width: busy ? "58%" : result ? "100%" : "0%" }} /></div>
          <ResultPanel emptyTitle={activeBrainId ? "No evidence yet" : "Select or create a brain first"} result={result} />
          <article className="context-transparency"><span>Transparency</span><strong>See what the brain relied on</strong><p>Every returned memory and source remains inspectable in the local result receipt.</p></article>
        </aside>
      </div>
    </section>
  );
}

function GrowRoute({
  activeBrainId,
  activity,
  busy,
  nodes,
  onFileChange,
  onRun,
  result,
  setSourceKind,
  setSourceLabel,
  setSourceUrl,
  setSourceText,
  sourceFileName,
  sourceKind,
  sourceLabel,
  sourceText,
  sourceUrl,
}: {
  activeBrainId: string;
  activity: BrainActivity;
  busy: boolean;
  nodes: GraphNode[];
  onFileChange: (file: File | null) => void;
  onRun: () => void;
  result: Record<string, unknown> | null;
  setSourceKind: (value: GrowSourceKind) => void;
  setSourceLabel: (value: string) => void;
  setSourceUrl: (value: string) => void;
  setSourceText: (value: string) => void;
  sourceFileName: string;
  sourceKind: GrowSourceKind;
  sourceLabel: string;
  sourceText: string;
  sourceUrl: string;
}) {
  const requiresUrl = sourceKind === "url" || sourceKind === "website";
  const sourceReady = requiresUrl ? sourceUrl.trim().length > 0 : sourceText.trim().length > 0;
  const preview = growPreviewSummary(result);
  const previewReady = preview.sourceUnits !== "0" || preview.candidates !== "0";
  return (
    <div className="grow-product">
      <section className="grow-overview" aria-label="Grow operation status">
        <div>
          <PanelEyebrow icon={Sparkles} label="Guided growth" />
          <h2>Grow Workspace</h2>
          <p>Turn local source material into inspectable memory candidates, then approve the exact set before any write.</p>
        </div>
        <div className="grow-overview-metrics">
          <Receipt title="Source units" detail={preview.sourceUnits} tone={preview.sourceUnits === "0" ? "pending" : "ready"} />
          <Receipt title="Ghost nodes" detail={preview.candidates} tone={preview.candidates === "0" ? "pending" : "active"} />
          <Receipt title="Will add" detail={previewReady ? preview.candidates : "0"} tone={previewReady ? "active" : "pending"} />
          <Receipt title="Current state" detail={previewReady ? "Review candidates" : "Preview required"} tone={previewReady ? "ready" : "pending"} />
        </div>
      </section>

      <div className="grow-workbench">
        <aside className="grow-runway" aria-label="Grow steps">
          <PanelEyebrow icon={GitBranch} label="Operator runway" />
          {[
            { label: "Prepare source", detail: sourceReady ? "source ready" : "text, URL, website or upload", state: sourceReady ? "done" : "active" },
            { label: "Preview formation", detail: previewReady ? "preview ready" : "not run", state: previewReady ? "done" : sourceReady ? "active" : "pending" },
            { label: "Inspect candidates", detail: previewReady ? `${preview.candidates} proposed` : "waiting for preview", state: previewReady ? "active" : "pending" },
            { label: "Apply growth", detail: previewReady ? "explicit review required" : "locked until review", state: "pending" },
          ].map((step, index) => (
            <article className={step.state} key={step.label}>
              <span>{step.state === "done" ? <CheckCircle2 size={15} /> : index + 1}</span>
              <div>
                <strong>{step.label}</strong>
                <small>{step.detail}</small>
              </div>
            </article>
          ))}
        </aside>

        <div className="operation-grid grow-operation-grid">
          <section className="command-surface grow-workspace">
            <div className="grow-step-heading">
              <span>Step 1</span>
              <strong>Add source material</strong>
              <small>Nothing is written during preview.</small>
            </div>
        <div className="grow-source-mode-grid" role="radiogroup" aria-label="Source type">
          {growSourceModes.map((mode) => (
            <button
              aria-checked={sourceKind === mode.kind}
              className={sourceKind === mode.kind ? "active" : ""}
              key={mode.kind}
              onClick={() => setSourceKind(mode.kind)}
              role="radio"
              type="button"
            >
              <mode.icon size={16} />
              <strong>{mode.label}</strong>
              <span>{mode.meta}</span>
            </button>
          ))}
        </div>
        <div className="grow-input-grid">
          <label>
            Source label
            <input onChange={(event) => setSourceLabel(event.target.value)} placeholder="Interview notes, project page, PDF title" value={sourceLabel} />
          </label>
          {requiresUrl ? (
            <label>
              {sourceKind === "website" ? "Website URL" : "Source URL"}
              <input
                onChange={(event) => setSourceUrl(event.target.value)}
                placeholder={sourceKind === "website" ? "https://example.com/about" : "https://example.com/source.pdf"}
                type="url"
                value={sourceUrl}
              />
            </label>
          ) : (
            <label className="file-picker">
              Upload source
              <span>
                <UploadCloud size={15} />
                {sourceFileName || "PDF, DOCX, text, transcript or notes"}
              </span>
              <input
                accept=".txt,.md,.markdown,.json,.csv,.pdf,.docx,.png,.jpg,.jpeg,.webp,text/*,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,image/*"
                onChange={(event) => onFileChange(event.currentTarget.files?.[0] || null)}
                type="file"
              />
            </label>
          )}
        </div>
        <textarea
          aria-label="Source material"
          onChange={(event) => setSourceText(event.target.value)}
          placeholder={
            requiresUrl
              ? "Optional notes or instructions for this URL source."
              : "Paste source material. For PDFs/OCR sources, paste extracted text when running fully local Core."
          }
          value={sourceText}
        />
        <div className="grow-review-strip">
          <Receipt title="Source units" detail={preview.sourceUnits} tone={preview.sourceUnits === "0" ? "pending" : "ready" } />
          <Receipt title="Candidate nodes" detail={preview.candidates} tone={preview.candidates === "0" ? "pending" : "active" } />
          <Receipt title="Apply" detail={preview.applyState} tone={preview.applyState === "review needed" ? "pending" : "ready"} />
        </div>
        <button className="primary wide" disabled={busy || !sourceReady} onClick={onRun} type="button">
          {busy ? <RefreshCw size={17} /> : <GitBranch size={17} />}
          Preview memory growth
        </button>
        <p className="fine-print">
          Grow produces reviewable source units, ghost nodes, candidate nodes and an apply contract. Local Core preview uses no Detwin credits.
        </p>
          </section>
          <div className="live-result-stack">
            <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
            <GrowResultPanel emptyTitle={activeBrainId ? "Grow preview has not run" : "Create a brain before growing memory"} result={result} />
          </div>
        </div>
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
          Tool catalog: {mcpRegistry?.registry_status === "ready" ? "loaded" : mcpRegistry?.registry_status || "not loaded"}.
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

function ResultsRoute({ nodes, result }: { nodes: GraphNode[]; result: Record<string, unknown> | null }) {
  return (
    <div className="operation-grid">
      <section className="command-surface">
        <PanelEyebrow icon={BarChart3} label="Local result history" />
        <h2>Review the latest Core receipt.</h2>
        <p>Context, Grow, MCP and health operations publish their latest bounded response here without creating a cloud history dependency.</p>
        <MetricGrid metrics={[{ label: "Graph nodes", value: String(nodes.length) }, { label: "Latest receipt", value: result ? "available" : "not run" }, { label: "Storage", value: "local" }, { label: "Cloud credits", value: "not used" }]} />
      </section>
      <ResultPanel emptyTitle="Run a Local Core workflow to create a receipt" result={result} />
    </div>
  );
}

function BenchRoute({ activeBrainId, graph, health }: { activeBrainId: string; graph: GraphResponse | null; health: HealthState | null }) {
  return (
    <div className="operation-grid">
      <section className="command-surface">
        <PanelEyebrow icon={Activity} label="Local readiness bench" />
        <h2>Reproducible checks stay explicit.</h2>
        <p>Use the public MCP contract to run retrieval and memory checks against the selected local brain.</p>
        <MetricGrid metrics={[{ label: "Runtime", value: health?.ok ? "ready" : "offline" }, { label: "Brain", value: activeBrainId || "required" }, { label: "Graph", value: graph?.graph?.meta?.load_error ? "load error" : "available" }, { label: "Writes", value: "review gated" }]} />
      </section>
      <aside className="proof-panel">
        <Receipt title="Health first" detail="Verify the runtime and active brain before comparing retrieval behavior." tone={health?.ok ? "ready" : "pending"} />
        <Receipt title="Real-node evidence" detail="Brain Core never fills an empty graph with synthetic memory nodes." tone="active" />
        <Receipt title="Public boundary" detail="Bench actions use public Core contracts only." tone="ready" />
      </aside>
    </div>
  );
}

function CloudMaintainRoute() {
  return (
    <section className="cloud-handoff-workspace">
      <Activity size={28} />
      <div><span>Cloud-backed workflow</span><h2>Maintain is visible, but not executable in Local Core.</h2><p>Sleep, evolve and calibration source is not included in this public checkout. Use the cloud handoff to run account- and credit-gated maintenance.</p></div>
      <a className="secondary link-button" href={`${cloudUrl}/modules`}><Cloud size={16} />Open Maintain in Detwin Cloud</a>
    </section>
  );
}

function BrainSyncRoute({ activeBrain, activeBrainId }: { activeBrain: BrainSummary | null; activeBrainId: string }) {
  return (
    <div className="operation-grid">
      <section className="command-surface">
        <PanelEyebrow icon={CloudUpload} label="Explicit Brain Sync" />
        <h2>{activeBrain ? brainName(activeBrain) : "Select a local brain first."}</h2>
        <p>Local brains remain on this device until an explicit Detwin Cloud sync handoff is opened.</p>
        <MetricGrid metrics={[{ label: "Brain id", value: activeBrainId || "not selected" }, { label: "Local state", value: activeBrain ? "available" : "required" }, { label: "Automatic upload", value: "off" }, { label: "Cloud account", value: "optional" }]} />
      </section>
      <aside className="proof-panel">
        <Receipt title="Local by default" detail="No brain archive is transmitted by this standalone UI." tone="ready" />
        <Receipt title="Explicit handoff" detail="Cloud sync starts only after opening Detwin and authorizing the account workflow there." tone="active" />
        <a className="secondary link-button" href={`${cloudUrl}/brains`}><CloudUpload size={16} />Open Brain Sync</a>
      </aside>
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

function SettingsRoute({
  activeBrainId,
  health,
  mcpRegistry,
  setTheme,
  theme,
}: {
  activeBrainId: string;
  health: HealthState | null;
  mcpRegistry: McpRegistry | null;
  setTheme: (theme: ThemeMode) => void;
  theme: ThemeMode;
}) {
  return (
    <div className="settings-grid">
      <Notice tone="ready" title="Local-first boundary" detail="This UI talks only to the local AGVM API configured with VITE_API_URL. It does not sign in, sync, bill, or unlock cloud modules." />
      <section className="settings-panel">
        <PanelEyebrow icon={Sun} label="Interface palette" />
        <h2>Match Detwin by default.</h2>
        <p>Core opens in the same light direction as the Platform. Switch to the dark brain cockpit when you want the high-contrast operator view.</p>
        <div className="theme-toggle" role="group" aria-label="Theme">
          <button className={theme === "light" ? "active" : ""} onClick={() => setTheme("light")} type="button">
            <Sun size={16} />
            Light
          </button>
          <button className={theme === "dark" ? "active" : ""} onClick={() => setTheme("dark")} type="button">
            <Moon size={16} />
            Dark
          </button>
        </div>
      </section>
      <MetricGrid
        metrics={[
          { label: "API base", value: apiBaseUrl },
          { label: "Cloud link", value: cloudUrl },
          { label: "Active brain", value: activeBrainId || "not selected" },
          { label: "MCP tools", value: String(mcpRegistry?.tools?.length || 0) },
          { label: "Brain list", value: health?.brain_registry_ready ? "ready" : "not ready" },
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
  const [density, setDensity] = useState<"focus" | "balanced" | "detailed" | "full">("balanced");
  const nodeLimit = density === "focus" ? 30 : density === "balanced" ? 90 : density === "detailed" ? 240 : nodes.length;
  const visibleNodes = nodes.slice(0, nodeLimit);
  const liveGraph = Boolean(activeBrainId && nodes.length);
  const theme = readTheme();
  const points = useMemo(() => visibleNodes.map((node, index) => nodePoint3d(node, index, visibleNodes.length)), [visibleNodes]);
  return (
    <section className={`brain-canvas ${variant} ${activity.active ? "is-active" : "is-idle"}`} aria-label="Local AGVM brain projection">
      <Canvas className="brain-three-canvas" camera={{ fov: variant === "stage" ? 42 : 48, position: [0, 0.16, variant === "stage" ? 5.2 : 5.8] }} dpr={[1, 1.75]}>
        <color args={[theme === "dark" ? "#071311" : "#f7faf9"]} attach="background" />
        <ambientLight intensity={0.68} />
        <directionalLight color="#f7fffb" intensity={1.2} position={[3.2, 4.5, 5]} />
        <pointLight color="#00e9b1" intensity={2.4} position={[-2.6, 1.8, 2.4]} />
        <pointLight color="#8b55e7" intensity={1.25} position={[2.8, -1.2, 2.2]} />
        <BrainThreeScene activity={activity} points={points} variant={variant} />
        <OrbitControls
          autoRotate
          autoRotateSpeed={activity.active ? 1.2 : 0.38}
          enableDamping
          enablePan={false}
          enableZoom={false}
          maxPolarAngle={Math.PI * 0.72}
          minPolarAngle={Math.PI * 0.28}
        />
      </Canvas>
      <div className="brain-hud top-left">
        <span>{liveGraph ? "Active brain" : activeBrainId ? "Empty brain" : "Brain required"}</span>
        <strong>{liveGraph ? activeBrainId : activeBrainId ? "Grow the first memory" : "Create or import a brain"}</strong>
      </div>
      {!liveGraph ? (
        <div className="brain-empty-state">
          <Brain size={28} />
          <strong>{activeBrainId ? "Brain Core is ready for its first memory" : "Brain preview waiting for data"}</strong>
          <span>{activeBrainId ? "Use Grow to preview real memory candidates." : "Create, import or select a brain to load live nodes into this 3D field."}</span>
        </div>
      ) : null}
      <div className="brain-density-controls" aria-label="Brain node density">
        <span>Brain detail</span>
        {(["focus", "balanced", "detailed", "full"] as const).map((option) => (
          <button aria-pressed={density === option} key={option} onClick={() => setDensity(option)} type="button">
            {option[0].toUpperCase() + option.slice(1)}
          </button>
        ))}
      </div>
      <div className="brain-hud bottom-right">
        <span>{activity.label}</span>
        <strong>{activity.detail}</strong>
      </div>
      <div className="brain-hud bottom-left">
        <span>Graph nodes</span>
        <strong>{liveGraph ? `${visibleNodes.length} rendered` : "0 - no synthetic data"}</strong>
      </div>
    </section>
  );
}

function BrainThreeScene({
  activity,
  points,
  variant,
}: {
  activity: BrainActivity;
  points: BrainPoint3d[];
  variant: "stage" | "compact";
}) {
  const groupRef = useRef<Group>(null);
  const active = activity.active;
  const scale = variant === "stage" ? 1.14 : 1;
  const pathPoints = useMemo(() => points.filter((_, index) => index % 5 === 0 || index % 7 === 0).slice(0, 18), [points]);

  useFrame(({ clock }) => {
    if (!groupRef.current) return;
    const elapsed = clock.getElapsedTime();
    groupRef.current.rotation.y = Math.sin(elapsed * 0.18) * 0.16 + elapsed * (active ? 0.09 : 0.035);
    groupRef.current.rotation.x = -0.08 + Math.sin(elapsed * 0.22) * 0.035;
  });

  return (
    <group ref={groupRef} scale={scale}>
      {points.length >= 8 ? <group>
        <mesh position={[-0.72, 0.08, 0]} rotation={[0.04, 0.02, -0.08]} scale={[1.18, 0.78, 0.54]}>
          <sphereGeometry args={[1, 48, 24]} />
          <meshStandardMaterial color="#0e2b28" emissive="#00e9b1" emissiveIntensity={0.08} metalness={0.08} opacity={Math.min(0.16, 0.035 + points.length / 900)} roughness={0.72} transparent wireframe />
        </mesh>
        <mesh position={[0.72, 0.08, 0]} rotation={[0.04, -0.02, 0.08]} scale={[1.18, 0.78, 0.54]}>
          <sphereGeometry args={[1, 48, 24]} />
          <meshStandardMaterial color="#17122a" emissive="#8b55e7" emissiveIntensity={0.08} metalness={0.08} opacity={Math.min(0.16, 0.035 + points.length / 900)} roughness={0.72} transparent wireframe />
        </mesh>
        <mesh position={[0, -0.45, -0.08]} rotation={[0.05, 0, 0]} scale={[0.8, 0.32, 0.38]}>
          <sphereGeometry args={[1, 36, 18]} />
          <meshStandardMaterial color="#ded5ed" opacity={Math.min(0.1, 0.02 + points.length / 1200)} roughness={0.8} transparent wireframe />
        </mesh>
      </group> : null}

      {pathPoints.length > 1 ? (
        <group>
          {pathPoints.slice(1).map((point, index) => {
            const previous = pathPoints[index];
            return <ConnectionTube active={active} from={previous.position} key={`${previous.id}-${point.id}`} to={point.position} />;
          })}
        </group>
      ) : null}

      {points.map((point, index) => (
        <MemoryNodeMesh active={active} key={`${point.id}-${index}`} point={point} pulseOffset={index * 0.137} />
      ))}
    </group>
  );
}

function MemoryNodeMesh({ active, point, pulseOffset }: { active: boolean; point: BrainPoint3d; pulseOffset: number }) {
  const meshRef = useRef<Group>(null);
  useFrame(({ clock }) => {
    if (!meshRef.current) return;
    const pulse = 1 + Math.sin(clock.getElapsedTime() * (active ? 3.2 : 1.4) + pulseOffset) * (active ? 0.22 : 0.08);
    meshRef.current.scale.setScalar(pulse);
  });
  return (
    <group ref={meshRef} position={point.position}>
      <mesh>
        <sphereGeometry args={[point.size, 16, 10]} />
        <meshStandardMaterial color={point.color} emissive={point.color} emissiveIntensity={active ? 0.55 : 0.24} roughness={0.48} />
      </mesh>
      <mesh scale={2.15}>
        <sphereGeometry args={[point.size, 12, 8]} />
        <meshBasicMaterial color={point.color} opacity={0.11} transparent />
      </mesh>
    </group>
  );
}

function ConnectionTube({ active, from, to }: { active: boolean; from: [number, number, number]; to: [number, number, number] }) {
  const midpoint: [number, number, number] = [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2, (from[2] + to[2]) / 2];
  const dx = to[0] - from[0];
  const dy = to[1] - from[1];
  const dz = to[2] - from[2];
  const length = Math.sqrt(dx * dx + dy * dy + dz * dz);
  const quaternion = useMemo(() => {
    if (length < 0.001) return new Quaternion();
    const direction = new Vector3(dx, dy, dz).normalize();
    const axis = new Vector3(0, 1, 0);
    return new Quaternion().setFromUnitVectors(axis, direction);
  }, [dx, dy, dz, length]);
  if (length < 0.001) return null;
  return (
    <mesh position={midpoint} quaternion={quaternion}>
      <cylinderGeometry args={[active ? 0.008 : 0.005, active ? 0.008 : 0.005, length, 8, 1]} />
      <meshBasicMaterial color={active ? "#00e9b1" : "#ded5ed"} opacity={active ? 0.42 : 0.2} transparent />
    </mesh>
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
            <button disabled={busy} onClick={onBootstrap} type="button"><RefreshCw size={15} />Scan local brains</button>
            <button disabled={busy || !activeBrainId} onClick={onExportBrain} type="button"><Download size={15} />Export active</button>
            <button disabled={busy} onClick={onRefresh} type="button"><RefreshCw size={15} />Refresh</button>
            <a href="#brain_explorer"><Brain size={15} />Open Brain Explorer</a>
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
        <p>Context, Grow and MCP are brain-scoped. Scan discovers existing local brains; Create starter brain creates an empty brain ready for your first Grow.</p>
      </div>
      <button className="primary" disabled={busy} onClick={onCreateDemo} type="button"><PlusCircle size={16} />Create starter brain</button>
      <button className="secondary" disabled={busy} onClick={onBootstrap} type="button"><RefreshCw size={16} />Scan local brains</button>
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

function GrowResultPanel({ emptyTitle, result }: { emptyTitle: string; result: Record<string, unknown> | null }) {
  const summary = growPreviewSummary(result);
  const candidates = growCandidateSummaries(result);
  return (
    <section className="result-panel grow-result-panel">
      <PanelEyebrow icon={GitBranch} label="Grow preview" />
      {result ? (
        <>
          <div className="grow-result-kpis">
            <Receipt title="Source units" detail={summary.sourceUnits} tone={summary.sourceUnits === "0" ? "pending" : "ready"} />
            <Receipt title="Candidate nodes" detail={summary.candidates} tone={summary.candidates === "0" ? "pending" : "active"} />
            <Receipt title="Write state" detail={summary.applyState} tone={summary.applyState === "review needed" ? "pending" : "ready"} />
          </div>
          {candidates.length ? (
            <div className="grow-candidate-list">
              {candidates.map((candidate, index) => (
                <article key={`${candidate.title}-${index}`}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <div>
                    <strong>{candidate.title}</strong>
                    <p>{candidate.detail}</p>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="empty-result compact">
              <Network size={22} />
              <strong>No candidate list returned yet</strong>
              <span>Run a source preview with enough source material to inspect proposed memory nodes before apply.</span>
            </div>
          )}
          <details className="raw-receipt">
            <summary>Open exact receipt</summary>
            <pre>{JSON.stringify(result, null, 2)}</pre>
          </details>
        </>
      ) : (
        <div className="empty-result">
          <GitBranch size={26} />
          <strong>{emptyTitle}</strong>
          <span>The preview will show source units, candidate memory nodes and the exact apply contract.</span>
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

async function writeApiWithFallback<T>(primaryPath: string, fallbackPath: string, body: Record<string, unknown>): Promise<T> {
  try {
    return await writeApi<T>(primaryPath, body);
  } catch {
    return writeApi<T>(fallbackPath, body);
  }
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
  const raw = window.location.hash.replace(/^#/, "") || new URLSearchParams(window.location.search).get("route") || "context";
  return routes.some((item) => item.id === raw) ? (raw as RouteId) : "context";
}

function readTheme(): ThemeMode {
  try {
    const stored = window.localStorage?.getItem("agvm.core.theme");
    return stored === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
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
  if (route === "context") return "Retrieve from local memory.";
  if (route === "results") return "Inspect bounded Local Core results.";
  if (route === "brain_explorer") return activeBrain ? brainName(activeBrain) : "Explore a real local brain.";
  if (route === "grow") return "Grow remains local and explicit.";
  if (route === "mcp") return "Inspect and invoke raw Core MCP tools.";
  if (route === "modules") return "Core here. Advanced workflows in Cloud.";
  if (route === "maintain") return "Maintain runs through Detwin Cloud.";
  if (route === "health") return "Prove the runtime before changing memory.";
  if (route === "bench") return "Benchmark local memory with reproducible checks.";
  if (route === "brain_sync") return "Sync remains explicit and account-controlled.";
  return "Local settings stay on this machine.";
}

function descriptionForRoute(route: RouteId) {
  if (route === "context") return "Ask the selected local brain for a context package and inspect the receipt returned by the local API.";
  if (route === "results") return "Review the latest local operation response without creating a cloud history dependency.";
  if (route === "brain_explorer") return "Inspect the same real-node-only Brain Core projection used throughout Local AGVM.";
  if (route === "grow") return "Preview source growth through the local MCP contract. Nothing is applied without an explicit tool call.";
  if (route === "mcp") return "Load the local MCP catalog, select a contract and execute it directly against the Core server.";
  if (route === "modules") return "Grow is included in AGVM Core. Paid workflows are visible as non-executable cloud handoffs.";
  if (route === "maintain") return "Local Core exposes no paid maintenance runtime or executable source.";
  if (route === "health") return "Run health proof against the selected brain and keep the result separate from cloud readiness.";
  if (route === "bench") return "Use public Core contracts to compare runtime, graph and retrieval readiness.";
  if (route === "brain_sync") return "Keep local brains on-device until an explicit cloud sync workflow is authorized.";
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

function nodePoint3d(node: GraphNode, index: number, total: number): BrainPoint3d {
  const position = node.final_position || {};
  const hasRuntimePosition =
    typeof position.x === "number" || typeof position.y === "number" || typeof position.z === "number";
  const color = node.semantic_color?.hex || detwinNodeColor(index, node.memory_type);
  if (hasRuntimePosition) {
    return {
      id: String(node.id || `node-${index + 1}`),
      label: String(node.summary || node.id || "memory node"),
      memoryType: String(node.memory_type || "memory"),
      position: [
        clamp(Number(position.x || 0) * 1.78, -2.25, 2.25),
        clamp(Number(position.y || 0) * 1.2, -1.18, 1.18),
        clamp(Number(position.z || 0) * 1.12, -1.08, 1.08),
      ],
      color,
      size: 0.028 + (index % 5) * 0.006,
    };
  }

  const count = Math.max(total, 1);
  const theta = (index / count) * Math.PI * 2;
  const layer = index % 6;
  const hemisphere = index % 2 === 0 ? -1 : 1;
  const lobe = Math.floor(index / 2) % 5;
  const radial = 0.44 + (layer / 5) * 0.74;
  const fold = Math.sin(theta * 3 + lobe * 0.72) * 0.18;
  return {
    id: String(node.id || `demo-node-${index + 1}`),
    label: String(node.summary || node.id || "demo memory node"),
    memoryType: String(node.memory_type || "demo"),
    position: [
      hemisphere * (0.22 + Math.abs(Math.cos(theta)) * radial) + fold * 0.34,
      Math.sin(theta * 0.92) * (0.42 + lobe * 0.05) + Math.cos(theta * 2.4) * 0.08,
      Math.sin(theta * 1.47 + layer) * 0.46 + hemisphere * 0.08,
    ],
    color,
    size: 0.032 + (index % 5) * 0.006,
  };
}

function detwinNodeColor(index: number, memoryType?: string | null) {
  const type = String(memoryType || "").toLowerCase();
  if (type.includes("receipt") || type.includes("proof")) return "#8b55e7";
  if (type.includes("source") || type.includes("evidence")) return "#ded5ed";
  if (type.includes("growth") || type.includes("candidate")) return "#00e9b1";
  return ["#00e9b1", "#8b55e7", "#ded5ed", "#f7fffb"][index % 4];
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

function formatBytes(size: number) {
  if (!Number.isFinite(size) || size <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

function inputKindForFile(file: File): GrowSourceKind {
  const name = file.name.toLowerCase();
  const type = file.type.toLowerCase();
  if (name.endsWith(".pdf") || type === "application/pdf") return "pdf";
  if (name.endsWith(".docx") || type.includes("wordprocessingml")) return "docx";
  if (/\.(png|jpg|jpeg|webp|tiff|bmp)$/i.test(name) || type.startsWith("image/")) return "mixed_bundle";
  if (/\.(vtt|srt|transcript|txt)$/i.test(name)) return "transcript";
  return "manual_text";
}

function growRawInput(kind: GrowSourceKind, text: string, url: string, fileName: string) {
  if (kind === "url" || kind === "website") {
    return [url.trim(), text.trim()].filter(Boolean).join("\n\nNotes:\n");
  }
  if (fileName && text.trim()) return `${fileName}\n\n${text.trim()}`;
  return text.trim();
}

function growPreviewSummary(result: Record<string, unknown> | null) {
  const data = resultData(result);
  const sourceInvestigation = objectAt(data, "source_investigation") || objectAt(data, "sourceInvestigation");
  const previewBundle = objectAt(data, "preview_bundle") || objectAt(data, "previewBundle");
  const completeness = objectAt(data, "completeness") || objectAt(previewBundle, "completeness");
  const sourceUnits =
    arrayLength(sourceInvestigation?.source_units) ||
    numberString(sourceInvestigation?.source_unit_count) ||
    numberString(completeness?.source_unit_count) ||
    "0";
  const candidates =
    arrayLength(previewBundle?.derived_nodes) ||
    arrayLength(previewBundle?.candidate_nodes) ||
    numberString(completeness?.preview_node_count) ||
    (previewBundle?.primary_node_preview ? "1" : "0");
  const applyContract = objectAt(data, "source_formation_contract") || objectAt(data, "apply_contract") || objectAt(previewBundle, "apply_contract");
  const applyState = result?.status === "applied" ? "applied" : applyContract ? "review needed" : "preview first";
  return { applyState, candidates, sourceUnits };
}

function growCandidateSummaries(result: Record<string, unknown> | null) {
  const data = resultData(result);
  const previewBundle = objectAt(data, "preview_bundle") || objectAt(data, "previewBundle");
  const rawCandidates = arrayAt(previewBundle, "derived_nodes") || arrayAt(previewBundle, "candidate_nodes") || [];
  const candidates = rawCandidates
    .map((item) => (item && typeof item === "object" ? (item as Record<string, unknown>) : null))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .slice(0, 8)
    .map((item) => ({
      title: String(item.summary || item.title || item.label || item.node_id || "Candidate memory node"),
      detail: String(item.memory_type || item.source_label || item.confidence || item.rationale || "Review this candidate before any apply step."),
    }));
  if (!candidates.length && previewBundle?.primary_node_preview && typeof previewBundle.primary_node_preview === "object") {
    const primary = previewBundle.primary_node_preview as Record<string, unknown>;
    candidates.push({
      title: String(primary.summary || primary.title || "Primary candidate memory node"),
      detail: String(primary.memory_type || primary.rationale || "Primary preview returned by the local Grow contract."),
    });
  }
  return candidates;
}

function resultData(result: Record<string, unknown> | null) {
  if (!result) return null;
  const structured = objectAt(result, "structuredContent");
  const data = objectAt(structured, "data");
  return data || result;
}

function objectAt(source: unknown, key: string): Record<string, unknown> | null {
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const value = (source as Record<string, unknown>)[key];
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function arrayAt(source: unknown, key: string): unknown[] | null {
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const value = (source as Record<string, unknown>)[key];
  return Array.isArray(value) ? value : null;
}

function arrayLength(value: unknown) {
  return Array.isArray(value) ? String(value.length) : "";
}

function numberString(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "";
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
