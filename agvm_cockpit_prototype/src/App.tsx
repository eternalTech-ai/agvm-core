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
  Eye,
  EyeOff,
  FileUp,
  GitBranch,
  Globe2,
  HeartPulse,
  KeyRound,
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
  X,
} from "lucide-react";
import { Html, OrbitControls } from "@react-three/drei";
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
  migration_source?: string | null;
  lifecycle?: {
    bootstrap_state?: string | null;
    bootstrap_session_id?: string | null;
    profile_state?: string | null;
    benchmark_state?: string | null;
  } | null;
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
  | "brain_center"
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
const cloudUrl = String(
  import.meta.env.VITE_PLATFORM_URL
  || import.meta.env.VITE_DETWIN_CLOUD_URL
  || "https://app.detwin.ai",
).replace(/\/$/, "");
const connectedClientDeviceTokenKey = "agvm.platform.connected_client.device_token.v1";
const connectedClientPlatformOriginKey = "agvm.platform.connected_client.platform_origin.v1";

const routes: Array<{ id: RouteId; label: string; shortLabel?: string; eyebrow: string; icon: LucideIcon }> = [
  { id: "brain_center", label: "Brain Center", eyebrow: "Registry", icon: Brain },
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
  const [query, setQuery] = useState("");
  const [includeAnswerDemo, setIncludeAnswerDemo] = useState(false);
  const [retrievalLimit, setRetrievalLimit] = useState(24);
  const [theme, setTheme] = useState<ThemeMode>(() => readTheme());
  const [sourceKind, setSourceKind] = useState<GrowSourceKind>("manual_text");
  const [sourceLabel, setSourceLabel] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceFileName, setSourceFileName] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [newBrainDisplayName, setNewBrainDisplayName] = useState("");
  const [newBrainId, setNewBrainId] = useState("");
  const [importBrainDisplayName, setImportBrainDisplayName] = useState("");
  const [importBrainId, setImportBrainId] = useState("");
  const [toolName, setToolName] = useState("retrieve_context");
  const [rawPayload, setRawPayload] = useState("{\n  \"query_text\": \"What should AGVM retrieve from this brain?\"\n}");
  const [resultsByRoute, setResultsByRoute] = useState<Partial<Record<RouteId, Record<string, unknown>>>>({});
  const [bootstrapSession, setBootstrapSession] = useState<Record<string, unknown> | null>(null);

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

  useEffect(() => {
    document.querySelector<HTMLElement>(".agvm-product-main")?.scrollTo({ top: 0, behavior: "auto" });
  }, [route]);

  const brains = useMemo(() => registry?.brains || [], [registry]);
  const activeBrainId = String(
    registry?.active_brain_id || health?.active_brain_id || brains.find((brain) => brain.is_active)?.brain_id || brains[0]?.brain_id || "",
  );
  const activeBrain = brains.find((brain) => brainId(brain) === activeBrainId) || brains[0] || null;
  const bootstrapReady = isBootstrapReady(activeBrain);
  const nodes = graph?.graph?.nodes || [];
  const toolOptions = useMemo(() => (mcpRegistry?.tools || []).filter((tool) => tool.endpoint_path).slice(0, 80), [mcpRegistry]);
  const selectedTool = toolOptions.find((tool) => tool.name === toolName) || toolOptions[0] || null;
  const routeModel = routes.find((item) => item.id === route) || routes[0];
  const result = resultsByRoute[route] || null;
  const activity = activityFor(busyAction, route, result);

  const setResult = (next: Record<string, unknown> | null) => {
    setResultsByRoute((current) => {
      if (!next) {
        const updated = { ...current };
        delete updated[route];
        return updated;
      }
      return { ...current, [route]: next };
    });
  };

  useEffect(() => {
    if (loading || !activeBrainId || bootstrapReady || route === "brain_center") return;
    if (!window.location.hash || route === "context" || route === "grow") {
      setRoute("brain_center");
      window.location.hash = "brain_center";
    }
  }, [activeBrainId, bootstrapReady, loading, route]);

  useEffect(() => {
    if (selectedTool && selectedTool.name !== toolName) setToolName(selectedTool.name);
  }, [selectedTool, toolName]);

  async function runAction(action: string, executor: () => Promise<Record<string, unknown>>) {
    setBusyAction(action);
    setLastError(null);
    try {
      const response = await executor();
      setResult(response);
      return response;
    } catch (error: unknown) {
      setLastError(errorMessage(error));
      setResult(null);
      return null;
    } finally {
      setBusyAction(null);
    }
  }

  function createLocalBrain(displayName = newBrainDisplayName, requestedBrainId = newBrainId) {
    const cleanDisplayName = displayName.trim() || "Personal Memory";
    const brain_id = normalizeBrainId(requestedBrainId || cleanDisplayName);
    return runAction("create-brain", async () => {
      const payload = {
        brain_id,
        display_name: cleanDisplayName,
        description: "Created from the AGVM Core UI.",
        make_active: true,
        make_default: true,
      };
      const response = await writeApi<Record<string, unknown>>("/mcp/brains/create", payload);
      await refresh();
      setBootstrapSession(null);
      setRoute("brain_center");
      window.location.hash = "brain_center";
      return response;
    });
  }

  function bootstrapRegistry() {
    runAction("refresh-brain-registry", async () => {
      await refresh();
      return { status: "ok", operation: "brain_registry_refresh", mutation: "none" };
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
      return writeApi<Record<string, unknown>>("/mcp/brains/export", { brain_id: activeBrainId });
    });
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

  async function runBootstrapCommand(operation: string, payload: Record<string, unknown>) {
    const response = await runAction(`bootstrap-${operation}`, async () => {
      const next = await writeApi<Record<string, unknown>>(`/mcp/brain-bootstrap-${operation.replace(/_/g, "-")}`, {
        brain_id: activeBrainId || undefined,
        ...payload,
      });
      const session = objectAt(next, "session");
      if (session) setBootstrapSession(session);
      if (operation === "apply") await refresh();
      return next;
    });
    return response;
  }

  async function applyGrowPreview(selectedPreviewIds: string[]) {
    const data = resultData(result);
    const investigation = objectAt(data, "source_investigation");
    const investigationId = String(investigation?.investigation_id || "").trim();
    if (!investigationId) {
      setLastError("Run a Grow preview before applying reviewed candidates.");
      return null;
    }
    return runAction("grow-apply", async () => {
      const response = await writeApi<Record<string, unknown>>("/mcp/grow-source-apply", {
        brain_id: activeBrainId || undefined,
        investigation_id: investigationId,
        source_investigation: investigation || undefined,
        source_formation_contract: objectAt(data, "source_formation_contract") || undefined,
        preview_bundle: objectAt(data, "preview_bundle") || undefined,
        selected_preview_ids: selectedPreviewIds,
        approved_preview_ids: selectedPreviewIds,
        learning_mode: "strict_review",
        confirm_apply: true,
      });
      await refresh();
      return response;
    });
  }

  async function retrieveContext() {
    return runAction("retrieve", async () => {
      const first = await writeApi<Record<string, unknown>>("/mcp/retrieve-context", {
        brain_id: activeBrainId || undefined,
        query_text: query,
        retrieval_mode: "balanced",
        context_package_mode: "mcp_operational",
        document_text_policy: "refs_only",
        max_matches: retrievalLimit,
        include_answer_demo: includeAnswerDemo,
        include_raw_text: false,
        complete_paths: true,
      });
      setResult(first);
      let latest = first;
      const searchId = contextSearchId(first);
      for (let attempt = 0; searchId && !contextResultIsTerminal(latest) && attempt < 8; attempt += 1) {
        await waitFor(350 + attempt * 150);
        try {
          latest = await writeApi<Record<string, unknown>>("/mcp/inspect-context-package", { search_id: searchId });
          setResult(latest);
        } catch {
          // The first package remains usable while background materialization catches up.
        }
      }
      if (searchId) {
        try {
          const trace = await readApi<Record<string, unknown>>(
            withQuery(`/memory/get-trace/${encodeURIComponent(searchId)}`, { brain_id: activeBrainId || undefined }),
          );
          latest = {
            ...latest,
            ui_trace: {
              landing_metadata: arrayAt(trace, "landing_metadata"),
              context_waves: arrayAt(trace, "context_waves"),
              worker_stop_reasons: objectAt(trace, "worker_stop_reasons") || {},
            },
          };
          setResult(latest);
        } catch {
          // The structured context remains valid when optional trace enrichment is unavailable.
        }
      }
      return latest;
    });
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
          <div><strong>de.twin</strong><span>AGVM Local</span></div>
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
                const response = await writeApi<Record<string, unknown>>("/mcp/select-brain", { brain_id: brain, make_default: false });
                await refresh();
                setBootstrapSession(null);
                setResultsByRoute({});
                const selected = brains.find((item) => brainId(item) === brain) || null;
                if (!isBootstrapReady(selected)) {
                  setRoute("brain_center");
                  window.location.hash = "brain_center";
                }
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
          {route !== "brain_center" && route !== "context" && route !== "brain_explorer" ? (
            <header className="workspace-head agvm-product-page-header">
              <div><span>{routeModel.eyebrow.toUpperCase()}</span><h1>{headlineForRoute(route, activeBrain)}</h1><p>{descriptionForRoute(route)}</p></div>
              <div className="workspace-actions">
                <a className="secondary" href={`${cloudUrl}/modules`}><Cloud size={16} />Use Detwin Cloud</a>
              </div>
            </header>
          ) : null}
          {lastError ? (
            <Notice
              actionLabel={isProviderConfigurationError(lastError) ? "Configure provider" : undefined}
              detail={localRequestErrorDetail(lastError)}
              onAction={isProviderConfigurationError(lastError) ? () => {
                setRoute("settings");
                window.location.hash = "settings";
              } : undefined}
              title={isProviderConfigurationError(lastError) ? "Connect your AI provider to continue" : "Local request did not complete"}
              tone="blocked"
            />
          ) : null}
          {!activeBrainId && route !== "context" && route !== "brain_center" ? <BrainBootstrapNotice busyAction={busyAction} onBootstrap={bootstrapRegistry} /> : null}

          {route === "brain_center" ? (
            <BrainCenterRoute
              activeBrain={activeBrain}
              activeBrainId={activeBrainId}
              bootstrapReady={bootstrapReady}
              busyAction={busyAction}
              nodes={nodes}
              onCommand={runBootstrapCommand}
              onOpenContext={() => {
                setRoute("context");
                window.location.hash = "context";
              }}
              session={bootstrapSession}
            />
          ) : null}
          {route === "brain_explorer" ? <BrainRoute activeBrain={activeBrain} activeBrainId={activeBrainId} activity={activity} graph={graph} nodes={nodes} /> : null}
          {route === "context" ? (
            <ContextRoute
              activeBrainId={activeBrainId}
              activity={activity}
              bootstrapReady={bootstrapReady}
              busy={busyAction === "retrieve"}
              includeAnswerDemo={includeAnswerDemo}
              nodes={nodes}
              query={query}
              retrievalLimit={retrievalLimit}
              result={result}
              setIncludeAnswerDemo={setIncludeAnswerDemo}
              setQuery={setQuery}
              setRetrievalLimit={setRetrievalLimit}
              onOpenBootstrap={() => {
                setRoute("brain_center");
                window.location.hash = "brain_center";
              }}
              onConfigureProvider={() => {
                setRoute("settings");
                window.location.hash = "settings";
              }}
              onRun={() => void retrieveContext()}
            />
          ) : null}
          {route === "results" ? <ResultsRoute nodes={nodes} result={result} /> : null}
          {route === "grow" ? (
            <GrowRoute
              activeBrainId={activeBrainId}
              activity={activity}
              bootstrapReady={bootstrapReady}
              busy={busyAction === "grow" || busyAction === "grow-apply"}
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
              onApply={applyGrowPreview}
              onConfigureProvider={() => {
                setRoute("settings");
                window.location.hash = "settings";
              }}
              onOpenBootstrap={() => {
                setRoute("brain_center");
                window.location.hash = "brain_center";
              }}
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
          {route === "brain_sync" ? <BrainSyncRoute activeBrain={activeBrain} activeBrainId={activeBrainId} nodes={nodes} onRefresh={refresh} /> : null}
          {route === "settings" ? <SettingsRoute activeBrainId={activeBrainId} health={health} mcpRegistry={mcpRegistry} setTheme={setTheme} theme={theme} /> : null}
          </section>
        </main>
      </div>
    </section>
  );
}

function BrainCenterRoute({
  activeBrain,
  activeBrainId,
  bootstrapReady,
  busyAction,
  nodes,
  onCommand,
  onOpenContext,
  session,
}: {
  activeBrain: BrainSummary | null;
  activeBrainId: string;
  bootstrapReady: boolean;
  busyAction: string | null;
  nodes: GraphNode[];
  onCommand: (operation: string, payload: Record<string, unknown>) => Promise<Record<string, unknown> | null | undefined>;
  onOpenContext: () => void;
  session: Record<string, unknown> | null;
}) {
  const [goal, setGoal] = useState("");
  const [interviewMode, setInterviewMode] = useState<"adaptive_ai" | "manual">("adaptive_ai");
  const [manualQuestions, setManualQuestions] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [sourceText, setSourceText] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const requestedStatus = useRef("");
  const state = bootstrapSessionState(session);
  const questions = arrayAt(session, "questions").map((item) => String(item || "")).filter(Boolean);
  const candidates = bootstrapCandidates(session);
  const sessionId = String(session?.session_id || activeBrain?.lifecycle?.bootstrap_session_id || "").trim();
  const revision = Number(session?.revision || 0);
  const busy = Boolean(busyAction?.startsWith("bootstrap-"));

  useEffect(() => {
    if (session || !sessionId || requestedStatus.current === sessionId) return;
    requestedStatus.current = sessionId;
    void onCommand("status", { session_id: sessionId });
  }, [onCommand, session, sessionId]);

  useEffect(() => {
    if (!candidates.length) return;
    const ids = candidates.map((candidate) => candidate.id);
    setSelectedIds((current) => current.length ? current.filter((id) => ids.includes(id)) : ids);
  }, [candidates.map((candidate) => candidate.id).join("|")]);

  if (!activeBrainId || !activeBrain) {
    return (
      <section className="brain-center-onboarding">
        <div className="brain-center-copy">
          <PanelEyebrow icon={Brain} label="Brain Center" />
          <h1>Create your first brain</h1>
          <p>Use Manage in the top bar to name a local brain or import a reviewed archive. Nothing is uploaded.</p>
        </div>
        <BrainCanvas activeBrainId="" activity={activityFor(null, "brain_center")} nodes={[]} />
      </section>
    );
  }

  if (bootstrapReady || state === "applied") {
    return (
      <section className="brain-center-onboarding brain-born">
        <div className="brain-center-copy">
          <PanelEyebrow icon={CheckCircle2} label="Brain ready" />
          <h1>{brainName(activeBrain)}</h1>
          <p>{nodes.length} reviewed memories are active. Context, Grow, Explorer and MCP retrieval are unlocked.</p>
          <button className="primary" onClick={onOpenContext} type="button"><Search size={16} />Open Context</button>
        </div>
        <BrainCanvas activeBrainId={activeBrainId} activity={activityFor(null, "brain_center")} nodes={nodes} />
      </section>
    );
  }

  return (
    <section className="bootstrap-workspace">
      <header className="bootstrap-hero">
        <div>
          <PanelEyebrow icon={Sparkles} label="Required Brain Bootstrap" />
          <h1>{state === "applied" ? "Your brain is alive" : `Build ${brainName(activeBrain)}`}</h1>
          <p>Define the mission, answer a domain-specific interview, add trusted material, review every memory and apply once.</p>
        </div>
        <div className="bootstrap-progress" aria-label="Bootstrap progress">
          {["Purpose", "Interview", "Foundations", "Review", "Activate"].map((label, index) => (
            <span className={bootstrapStepState(state, index)} key={label}><b>{index + 1}</b>{label}</span>
          ))}
        </div>
      </header>

      <div className="bootstrap-layout">
        <div className="bootstrap-form-stack">
          {!session ? (
            <section className="command-surface bootstrap-panel">
              <PanelEyebrow icon={Brain} label="Purpose" />
              <h2>What should this brain understand?</h2>
              <textarea aria-label="Brain purpose" onChange={(event) => setGoal(event.target.value)} placeholder="Describe the work, users, trusted sources and decisions this brain should support." value={goal} />
              <div className="theme-toggle bootstrap-mode" role="group" aria-label="Interview mode">
                <button className={interviewMode === "adaptive_ai" ? "active" : ""} onClick={() => setInterviewMode("adaptive_ai")} type="button"><Sparkles size={16} />AI interview</button>
                <button className={interviewMode === "manual" ? "active" : ""} onClick={() => setInterviewMode("manual")} type="button"><ClipboardCheck size={16} />Manual questions</button>
              </div>
              {interviewMode === "manual" ? (
                <label className="bootstrap-question-authoring">
                  Interview questions
                  <textarea aria-label="Manual interview questions" onChange={(event) => setManualQuestions(event.target.value)} placeholder="One question per line. Add as many as this brain needs; six answered dimensions are the minimum quality gate." value={manualQuestions} />
                </label>
              ) : <p className="fine-print">A configured local provider creates a bounded interview from this purpose. The question count adapts to domain complexity.</p>}
              <button
                className="primary wide"
                disabled={busy || !goal.trim() || (interviewMode === "manual" && manualQuestionList(manualQuestions).length < 6)}
                onClick={() => void onCommand("start", {
                  idempotency_key: `bootstrap-start-${activeBrainId}`,
                  goal: goal.trim(),
                  interview_mode: interviewMode,
                  quality_policy: "guided_seed_v1",
                  questions: interviewMode === "manual" ? manualQuestionList(manualQuestions) : undefined,
                })}
                type="button"
              >
                {busy ? <RefreshCw size={17} /> : <ArrowRight size={17} />}
                Start Brain Bootstrap
              </button>
            </section>
          ) : null}

          {session && state !== "applied" ? (
            <section className="command-surface bootstrap-panel">
              <PanelEyebrow icon={MessageSquareText} label="Human review interview" />
              <div className="bootstrap-panel-heading"><h2>Answer the questions that shape this brain</h2><strong>{arrayAt(session, "answers").length} / {questions.length}</strong></div>
              <div className="bootstrap-question-list">
                {questions.map((question, index) => {
                  const questionId = bootstrapQuestionId(question, index);
                  const saved = arrayAt(session, "answers").some((item) => objectAtValue(item, "question_id") === questionId);
                  return (
                    <article className={saved ? "saved" : ""} key={questionId}>
                      <span>{String(index + 1).padStart(2, "0")}</span>
                      <label>
                        <strong>{question}</strong>
                        <textarea aria-label={`Answer ${index + 1}`} disabled={saved} onChange={(event) => setAnswers((current) => ({ ...current, [questionId]: event.target.value }))} placeholder="Write a concrete, reviewable answer." value={answers[questionId] || ""} />
                      </label>
                      <button className="secondary" disabled={busy || saved || !String(answers[questionId] || "").trim()} onClick={() => void onCommand("answer", { session_id: sessionId, expected_revision: revision, idempotency_key: `bootstrap-answer-${sessionId}-${questionId}`, question_id: questionId, answer: String(answers[questionId] || "").trim() })} type="button">
                        {saved ? <CheckCircle2 size={15} /> : <ArrowRight size={15} />}{saved ? "Saved" : "Save answer"}
                      </button>
                    </article>
                  );
                })}
              </div>
            </section>
          ) : null}

          {session && state !== "applied" ? (
            <section className="command-surface bootstrap-panel">
              <PanelEyebrow icon={Database} label="Trusted foundations" />
              <h2>Add reviewed material</h2>
              <textarea aria-label="Bootstrap source material" onChange={(event) => setSourceText(event.target.value)} placeholder="Paste specifications, decisions, operating rules or other trusted source material. At least 240 characters are required by the quality gate." value={sourceText} />
              <button className="secondary wide" disabled={busy || sourceText.trim().length < 240} onClick={() => void onCommand("add_source", { session_id: sessionId, expected_revision: revision, idempotency_key: `bootstrap-source-${sessionId}-${revision}`, source_id: `bootstrap-source-${revision}`, source_label: "Brain Bootstrap reviewed material", source_kind: "manual_text", source_text: sourceText.trim(), source_trust: "user_asserted" })} type="button"><PlusCircle size={16} />Add reviewed source</button>
              <button className="primary wide" disabled={busy || arrayAt(session, "answers").length < Number(objectAt(session, "quality_requirements")?.minimum_answer_count || 6) || !arrayAt(session, "sources").length} onClick={() => void onCommand("preview", { session_id: sessionId, expected_revision: revision, idempotency_key: `bootstrap-preview-${sessionId}-${revision}`, capability: "grow_review" })} type="button"><GitBranch size={16} />Build memory preview</button>
            </section>
          ) : null}

          {candidates.length ? (
            <section className="command-surface bootstrap-panel bootstrap-review-panel">
              <PanelEyebrow icon={ClipboardCheck} label="Review memories" />
              <div className="bootstrap-panel-heading"><h2>Approve the exact memories to activate</h2><strong>{selectedIds.length} / {candidates.length}</strong></div>
              <div className="bootstrap-candidate-list">
                {candidates.map((candidate, index) => (
                  <label className={selectedIds.includes(candidate.id) ? "selected" : ""} key={candidate.id}>
                    <input checked={selectedIds.includes(candidate.id)} onChange={(event) => setSelectedIds((current) => event.target.checked ? [...new Set([...current, candidate.id])] : current.filter((id) => id !== candidate.id))} type="checkbox" />
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <div><strong>{candidate.title}</strong><p>{candidate.detail}</p></div>
                  </label>
                ))}
              </div>
              <button className="primary wide" disabled={busy || selectedIds.length < 12} onClick={() => void onCommand("apply", { session_id: sessionId, expected_revision: revision, idempotency_key: `bootstrap-apply-${sessionId}`, selected_preview_ids: selectedIds, confirm_apply: true })} type="button"><Sparkles size={17} />Activate reviewed brain</button>
              <p className="fine-print">The guided seed requires 12-30 distinct, grounded memories. Only checked candidates cross the write boundary.</p>
            </section>
          ) : null}
        </div>

        <aside className="bootstrap-visual">
          <BrainCanvas activeBrainId={activeBrainId} activity={{ active: busy, detail: busy ? "forming reviewed memory" : state.replace(/_/g, " "), label: state === "applied" ? "Brain born" : "Bootstrap live", phase: busy ? "growing" : "idle" }} nodes={nodes} />
          <div className="bootstrap-quality-grid">
            <Receipt title="Answers" detail={String(arrayAt(session, "answers").length)} tone={arrayAt(session, "answers").length ? "ready" : "pending"} />
            <Receipt title="Sources" detail={String(arrayAt(session, "sources").length)} tone={arrayAt(session, "sources").length ? "ready" : "pending"} />
            <Receipt title="Candidates" detail={String(candidates.length)} tone={candidates.length >= 12 ? "active" : "pending"} />
            <Receipt title="Write state" detail={state === "applied" ? "activated" : "review gated"} tone={state === "applied" ? "ready" : "pending"} />
          </div>
        </aside>
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
  bootstrapReady,
  busy,
  includeAnswerDemo,
  nodes,
  onConfigureProvider,
  onOpenBootstrap,
  onRun,
  query,
  retrievalLimit,
  result,
  setIncludeAnswerDemo,
  setQuery,
  setRetrievalLimit,
}: {
  activeBrainId: string;
  activity: BrainActivity;
  bootstrapReady: boolean;
  busy: boolean;
  includeAnswerDemo: boolean;
  nodes: GraphNode[];
  onConfigureProvider: () => void;
  onOpenBootstrap: () => void;
  onRun: () => void;
  query: string;
  retrievalLimit: number;
  result: Record<string, unknown> | null;
  setIncludeAnswerDemo: (value: boolean) => void;
  setQuery: (value: string) => void;
  setRetrievalLimit: (value: number) => void;
}) {
  const canRetrieve = Boolean(activeBrainId && bootstrapReady);
  const providerBlock = providerBlockReason(result);
  return (
    <section className="context-core-workspace">
      <div className="context-command-bar">
        <Search size={17} />
        <input aria-label="Context query" onChange={(event) => setQuery(event.target.value)} placeholder="Ask the brain for context..." value={query} />
        <button className="primary" disabled={busy || !canRetrieve || !query.trim()} onClick={onRun} type="button">
          {busy ? <RefreshCw size={17} /> : <Play size={17} />}
          Run Context
        </button>
      </div>
      <div className="context-search-controls" aria-label="Context search controls">
        <div className="context-depth" role="group" aria-label="Search depth">
          {[
            { label: "Focus", value: 12 },
            { label: "Balanced", value: 24 },
            { label: "Deep", value: 48 },
            { label: "Forensic", value: 64 },
          ].map((option) => (
            <button aria-pressed={retrievalLimit === option.value} key={option.value} onClick={() => setRetrievalLimit(option.value)} type="button">
              <strong>{option.label}</strong><span>{option.value} max</span>
            </button>
          ))}
        </div>
        <div className="context-output-mode" role="group" aria-label="Context output">
          <button aria-pressed={!includeAnswerDemo} onClick={() => setIncludeAnswerDemo(false)} type="button"><Database size={15} />Context only</button>
          <button aria-pressed={includeAnswerDemo} onClick={() => setIncludeAnswerDemo(true)} type="button"><MessageSquareText size={15} />Draft answer</button>
        </div>
      </div>
      {!canRetrieve ? (
        <Notice
          detail={activeBrainId ? "Complete the reviewed Bootstrap before retrieval. The first applied memories make this brain usable." : "Create or import a brain, then complete its reviewed Bootstrap."}
          title="Brain Bootstrap required"
          tone="pending"
        />
      ) : null}
      {!canRetrieve ? <button className="primary context-bootstrap-action" onClick={onOpenBootstrap} type="button"><Sparkles size={16} />Open Brain Bootstrap</button> : null}
      <div className="context-live-layout">
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
        <aside className="context-insight-rail">
          <PanelEyebrow icon={Search} label="Search status" />
          <strong>{busy ? "Retrieval active" : result ? resultStatusLabel(result) : canRetrieve ? "Ready for a question" : "Bootstrap required"}</strong>
          <div className="context-progress" aria-label="Context progress"><i style={{ width: busy ? "58%" : result ? "100%" : "0%" }} /></div>
          {providerBlock ? (
            <Notice
              actionLabel="Configure provider"
              detail="Context requires a verified AI provider for landing and reranking. This run stopped before retrieval; the brain was not changed and no Detwin credits were used."
              onAction={onConfigureProvider}
              title="AI provider required"
              tone="blocked"
            />
          ) : null}
          <ResultPanel emptyTitle={activeBrainId ? "No evidence yet" : "Select or create a brain first"} result={result} />
        </aside>
      </div>
    </section>
  );
}

function GrowRoute({
  activeBrainId,
  activity,
  bootstrapReady,
  busy,
  nodes,
  onApply,
  onConfigureProvider,
  onFileChange,
  onOpenBootstrap,
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
  bootstrapReady: boolean;
  busy: boolean;
  nodes: GraphNode[];
  onApply: (selectedPreviewIds: string[]) => Promise<Record<string, unknown> | null | undefined>;
  onConfigureProvider: () => void;
  onFileChange: (file: File | null) => void;
  onOpenBootstrap: () => void;
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
  const previewReady = Number(preview.candidates || 0) > 0 && String(resultData(result)?.status || result?.status || "") !== "blocked";
  const providerBlock = providerBlockReason(result);
  const sourceEvidenceBlock = growSourceEvidenceBlock(result);
  if (!activeBrainId || !bootstrapReady) {
    return (
      <section className="bootstrap-gate-surface">
        <div>
          <PanelEyebrow icon={Lock} label="Grow locked" />
          <h2>Bootstrap this brain before adding new memory.</h2>
          <p>Grow extends a reviewed brain. Complete the initial interview, foundations and memory review first.</p>
          <button className="primary" onClick={onOpenBootstrap} type="button"><Sparkles size={16} />Open Brain Bootstrap</button>
        </div>
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
      </section>
    );
  }
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
          <Receipt title="Apply" detail={preview.applyState} tone={preview.applyState === "applied" ? "ready" : "pending"} />
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
            {sourceEvidenceBlock ? (
              <Notice
                detail="Detwin received the reference, but not verified page content. Add source notes or paste the relevant text so Grow can ground every candidate before the AI runs. Nothing was applied."
                title="Source content required"
                tone="blocked"
              />
            ) : providerBlock ? (
              <Notice
                actionLabel="Configure provider"
                detail="Grow stopped before candidate formation because no verified AI provider is available. Your source text is preserved; no preview was applied and no Detwin credits were used."
                onAction={onConfigureProvider}
                title="AI formation unavailable"
                tone="blocked"
              />
            ) : null}
            <GrowResultPanel busy={busy} emptyTitle="Grow preview has not run" onApply={onApply} result={result} />
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
    <div className="health-workspace">
      <section className="command-surface health-command-surface">
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

type BrainSyncDirection = "local_to_cloud" | "cloud_to_local";
type BrainSyncPhase = "idle" | "connecting" | "preflight" | "review" | "applying" | "checking" | "complete" | "error";
type BrainSyncAccount = { actorId: string; organizationId: string; workspaceId: string };

function BrainSyncRoute({
  activeBrain,
  activeBrainId,
  nodes,
  onRefresh,
}: {
  activeBrain: BrainSummary | null;
  activeBrainId: string;
  nodes: GraphNode[];
  onRefresh: () => Promise<void>;
}) {
  const [direction, setDirection] = useState<BrainSyncDirection>("local_to_cloud");
  const [phase, setPhase] = useState<BrainSyncPhase>("idle");
  const [account, setAccount] = useState<BrainSyncAccount | null>(null);
  const [cloudBrains, setCloudBrains] = useState<Record<string, unknown>[]>([]);
  const [cloudBrainId, setCloudBrainId] = useState("");
  const [targetLocalId, setTargetLocalId] = useState("");
  const [targetDisplayName, setTargetDisplayName] = useState("");
  const [overwriteExisting, setOverwriteExisting] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [operation, setOperation] = useState<Record<string, unknown> | null>(null);
  const [restore, setRestore] = useState<Record<string, unknown> | null>(null);
  const [localSnapshot, setLocalSnapshot] = useState<Record<string, unknown> | null>(null);
  const [receipt, setReceipt] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const bootstrapReady = isBootstrapReady(activeBrain) && (activeBrain?.node_count || nodes.length) > 0;
  const connected = Boolean(connectedClientTokenForPlatform(brainSyncPlatformOrigin()));
  const operationId = String(operation?.preview_operation_id || "");
  const operationAuthority = String(operation?.operation_authority || "");
  const binding = objectAt(operation, "binding");
  const materializationReceipt = findNestedText(operation, "materialization_receipt_sha256");
  const activationReceipt = findNestedText(operation, "activation_receipt_sha256");
  const operationState = String(operation?.state || operation?.status || "").toLowerCase();
  const terminal = ["acknowledged", "complete", "completed", "ready", "synced"].includes(operationState)
    || findNestedValue(operation, "sync_claim_allowed") === true;
  const estimatedCredits = findNestedNumber(operation, "estimated_credit_units", "estimated_credits", "reserved_units");
  const sourceNodeCount = Number(localSnapshot?.node_count || activeBrain?.node_count || nodes.length || 0);
  const sourceEdgeCount = Number(localSnapshot?.edge_count || 0);
  const sourceRevision = Number(localSnapshot?.source_revision || binding?.source_revision || 0);

  useEffect(() => {
    const baseId = normalizeBrainId(activeBrainId || "restored_cloud_memory");
    setTargetLocalId(`${baseId}_cloud_copy`);
    setTargetDisplayName(`${activeBrain ? brainName(activeBrain) : "Cloud memory"} local copy`);
  }, [activeBrain, activeBrainId]);

  useEffect(() => {
    const handoff = new URLSearchParams(window.location.search).get("agvm_connected_client_handoff")?.trim() || "";
    if (!handoff) return;
    setPhase("connecting");
    exchangeConnectedClientHandoff(handoff)
      .then(() => {
        const url = new URL(window.location.href);
        url.searchParams.delete("agvm_connected_client_handoff");
        window.history.replaceState({}, "", url.toString());
        setPhase("idle");
      })
      .catch((caught) => {
        setError(syncErrorMessage(caught));
        setPhase("error");
      });
  }, []);

  const resetDirection = (next: BrainSyncDirection) => {
    setDirection(next);
    setPhase("idle");
    setOperation(null);
    setRestore(null);
    setReceipt(null);
    setConfirmed(false);
    setError("");
  };

  const loadCloudContext = async () => {
    if (!connectedClientTokenForPlatform(brainSyncPlatformOrigin())) {
      window.location.assign(connectedClientLoginUrl());
      return null;
    }
    const [profile, summary, registry] = await Promise.all([
      platformJson<Record<string, unknown>>("/v1/account/me"),
      platformJson<Record<string, unknown>>("/v1/account/brain-sync/summary"),
      platformJson<Record<string, unknown>>("/v1/account/hosted-mcp/brains"),
    ]);
    const nextAccount = brainSyncAccount(profile, summary);
    const hosted = arrayAt(registry, "brains").filter(isRecord);
    setAccount(nextAccount);
    setCloudBrains(hosted);
    setCloudBrainId((current) => current || String(registry.active_brain_id || hosted[0]?.brain_id || hosted[0]?.id || ""));
    return { account: nextAccount, cloudBrainId: String(registry.active_brain_id || hosted[0]?.brain_id || hosted[0]?.id || ""), cloudBrains: hosted };
  };

  const previewSync = async () => {
    setPhase("preflight");
    setConfirmed(false);
    setError("");
    try {
      const cloud = account ? { account, cloudBrainId, cloudBrains } : await loadCloudContext();
      if (!cloud) return;
      if (direction === "local_to_cloud") {
        if (!bootstrapReady || !activeBrainId) throw new Error("Complete Brain Bootstrap before syncing this local brain.");
        const source = await readApi<Record<string, unknown>>(`/memory/brains/${encodeURIComponent(activeBrainId)}/sync-snapshot?max_nodes=250000`);
        if (Number(source.node_count || 0) < 1) throw new Error("This brain has no reviewed memory to synchronize.");
        if (source.truncated === true) throw new Error("The snapshot exceeds the browser transfer boundary and was not uploaded.");
        const snapshot = objectAt(source, "snapshot");
        if (!snapshot) throw new Error("The local runtime did not return a complete graph snapshot.");
        const hash = snapshotHash(snapshot);
        const destinationBrainId = cloudBrainIdFor(activeBrainId);
        const existingDestination = cloud.cloudBrains.find((brain) => String(brain.brain_id || brain.id || "") === destinationBrainId);
        const expectedDestinationRevision = Number(existingDestination?.materialized_revision || existingDestination?.current_revision || existingDestination?.revision || existingDestination?.bootstrap_revision || 0);
        const result = await platformJson<Record<string, unknown>>("/memory/brains/sync/lifecycle/preview", {
          method: "POST",
          body: JSON.stringify({
            scope: { organization_id: cloud.account.organizationId, workspace_id: cloud.account.workspaceId },
            direction: "local_to_cloud",
            source_brain_id: activeBrainId,
            destination_brain_id: destinationBrainId,
            source_revision: Number(source.source_revision || 1),
            expected_destination_revision: expectedDestinationRevision,
            preview_idempotency_key: durableOperationKey(`brain-sync-preview:${activeBrainId}:${source.source_revision || 1}:${hash}:${expectedDestinationRevision}`),
            preview_ttl_seconds: 900,
            local_export: { ...source, snapshot },
            destination_display_name: `${String(source.display_name || (activeBrain ? brainName(activeBrain) : activeBrainId))} Cloud copy`,
            privacy_scope: "workspace_private",
          }),
        });
        setLocalSnapshot(source);
        setOperation(result);
        setReceipt(lastSyncReceipt(result));
      } else {
        const sourceCloudId = cloudBrainId || cloud.cloudBrainId;
        if (!sourceCloudId) throw new Error("Choose a Cloud brain to restore.");
        if (!targetLocalId.trim() || !targetDisplayName.trim()) throw new Error("Choose the new local brain ID and display name.");
        let result = await platformJson<Record<string, unknown>>("/v1/account/brain-sync/restore-bundle", {
          method: "POST",
          body: JSON.stringify({
            cloud_brain_id: sourceCloudId,
            target_local_brain_id: targetLocalId.trim(),
            target_display_name: targetDisplayName.trim(),
            overwrite_existing: overwriteExisting,
          }),
        });
        const envelope = objectAt(result, "bundle");
        const bundleId = String(envelope?.bundle_id || "");
        if (bundleId && !objectAt(envelope, "bundle")) {
          result = { ...result, ...(await platformJson<Record<string, unknown>>(`/v1/account/brain-sync/bundles/${encodeURIComponent(bundleId)}`)) };
        }
        const completeEnvelope = objectAt(result, "bundle");
        const restoreBundle = objectAt(completeEnvelope, "bundle");
        if (!restoreBundle) throw new Error("Detwin did not return a complete signed restore bundle.");
        const localPreflight = await writeApi<Record<string, unknown>>("/memory/brains/sync/preflight-restore", {
          schema_version: "agvm.core.brain_sync.preflight_restore_request.v1",
          bundle: restoreBundle,
        });
        setRestore({ ...result, local_preflight: localPreflight });
        setReceipt(objectAt(result, "receipt"));
      }
      setPhase("review");
    } catch (caught) {
      setError(syncErrorMessage(caught));
      setPhase("error");
    }
  };

  const applyReviewedSync = async () => {
    if (!confirmed) return;
    setPhase("applying");
    setError("");
    try {
      if (direction === "local_to_cloud") {
        if (!account || !operationId || !binding?.bundle_sha256 || !binding?.policy_sha256) {
          throw new Error("Run a fresh preflight before applying this Cloud copy.");
        }
        const result = await platformJson<Record<string, unknown>>(`/memory/brains/sync/lifecycle/${encodeURIComponent(operationId)}/apply`, {
          method: "POST",
          headers: syncAuthorityHeaders(operationAuthority),
          body: JSON.stringify({
            scope: { organization_id: account.organizationId, workspace_id: account.workspaceId },
            idempotency_key: durableOperationKey(`brain-sync-apply:${operationId}`),
            consent: {
              preview_operation_id: operationId,
              actor_id: account.actorId,
              bundle_sha256: binding.bundle_sha256,
              policy_sha256: binding.policy_sha256,
              granted: true,
            },
          }),
        });
        setOperation(result);
        setReceipt(lastSyncReceipt(result));
        setPhase(isTerminalSync(result) ? "complete" : "checking");
      } else {
        const restoreEnvelope = objectAt(restore, "bundle");
        const bundle = objectAt(restoreEnvelope, "bundle");
        const applyContract = objectAt(restore, "local_apply_contract");
        const restoreReceiptId = String(applyContract?.restore_receipt_id || receipt?.receipt_id || receipt?.id || "");
        if (!bundle || !restoreReceiptId) throw new Error("The signed restore bundle is incomplete. Run a fresh preview.");
        const bundleIdempotencyKey = String(bundle.idempotency_key || "");
        if (!bundleIdempotencyKey) throw new Error("The signed restore bundle is missing its idempotency binding. Run a fresh preview.");
        const localPreflight = objectAt(restore, "local_preflight");
        const destinationState = objectAt(localPreflight, "destination_state");
        const expectedDestinationStateSha256 = String(destinationState?.state_sha256 || "");
        if (!expectedDestinationStateSha256) throw new Error("The local destination was not bound during preflight. Run a fresh preview.");
        const localReceipt = await writeApi<Record<string, unknown>>("/memory/brains/sync/apply-restore", {
          schema_version: "agvm.core.brain_sync.apply_restore_request.v1",
          bundle,
          expected_destination_state_sha256: expectedDestinationStateSha256,
          idempotency_key: bundleIdempotencyKey,
          overwrite_existing_confirmed: overwriteExisting,
          select_after_restore: false,
        });
        const acknowledged = await platformJson<Record<string, unknown>>("/v1/account/brain-sync/restore-application", {
          method: "POST",
          body: JSON.stringify({ restore_receipt_id: restoreReceiptId, local_apply_receipt: localReceipt }),
        });
        setReceipt(acknowledged);
        await onRefresh();
        setPhase("complete");
      }
    } catch (caught) {
      setError(syncErrorMessage(caught));
      setPhase("error");
    }
  };

  const advanceCloudLifecycle = async () => {
    if (!account || !operationId) return;
    setPhase("checking");
    setError("");
    try {
      let result: Record<string, unknown>;
      if (materializationReceipt && !activationReceipt) {
        result = await platformJson<Record<string, unknown>>(`/memory/brains/sync/lifecycle/${encodeURIComponent(operationId)}/activation-review`, {
          method: "POST",
          headers: syncAuthorityHeaders(operationAuthority),
          body: JSON.stringify({
            scope: { organization_id: account.organizationId, workspace_id: account.workspaceId },
            actor_id: account.actorId,
            bundle_sha256: binding?.bundle_sha256,
            materialization_receipt_sha256: materializationReceipt,
            approved: true,
            note: "Approved from Local AGVM after verified materialization.",
          }),
        });
      } else if (activationReceipt) {
        result = await platformJson<Record<string, unknown>>(`/memory/brains/sync/lifecycle/${encodeURIComponent(operationId)}/acknowledge`, {
          method: "POST",
          headers: syncAuthorityHeaders(operationAuthority),
          body: JSON.stringify({
            scope: { organization_id: account.organizationId, workspace_id: account.workspaceId },
            actor_id: account.actorId,
            activation_receipt_sha256: activationReceipt,
            acknowledged: true,
          }),
        });
      } else {
        result = await platformJson<Record<string, unknown>>(`/memory/brains/sync/lifecycle/${encodeURIComponent(operationId)}`, {
          headers: syncAuthorityHeaders(operationAuthority),
        });
      }
      setOperation(result);
      setReceipt(lastSyncReceipt(result));
      setPhase(isTerminalSync(result) ? "complete" : "checking");
    } catch (caught) {
      setError(syncErrorMessage(caught));
      setPhase("error");
    }
  };

  if (!bootstrapReady && direction === "local_to_cloud") {
    return (
      <div className="brain-sync-route">
        <section className="brain-sync-intro">
          <div><PanelEyebrow icon={CloudUpload} label="Explicit Brain Sync" /><h2>Start locally or restore an existing Cloud brain.</h2><p>A reviewed local brain is required only for upload. Cloud restore remains available on a new installation.</p></div>
          <div className="brain-sync-direction" role="group" aria-label="Brain Sync direction">
            <button className="active" onClick={() => resetDirection("local_to_cloud")} type="button"><CloudUpload size={16} />Local to Cloud</button>
            <button onClick={() => resetDirection("cloud_to_local")} type="button"><Download size={16} />Cloud to device</button>
          </div>
        </section>
        <Notice tone="pending" title="Local upload needs Brain Bootstrap" detail="Create and activate reviewed memories, or choose Cloud to device to restore an existing Cloud brain into this installation." />
      </div>
    );
  }

  const stage = phase === "complete" ? 6 : phase === "checking" ? 5 : phase === "applying" ? 4 : phase === "review" ? 3 : phase === "preflight" || phase === "connecting" ? 2 : 1;
  const nextLabel = materializationReceipt && !activationReceipt
    ? "Activate verified Cloud brain"
    : activationReceipt
      ? "Record final receipt"
      : "Check original operation";

  return (
    <div className="brain-sync-route">
      <section className="brain-sync-intro">
        <div><PanelEyebrow icon={CloudUpload} label="Explicit Brain Sync" /><h2>Move a reviewed snapshot, never a live hidden stream.</h2><p>Local and Cloud remain separate until preflight, destination review and explicit confirmation are complete.</p></div>
        <div className="brain-sync-direction" role="group" aria-label="Brain Sync direction">
          <button className={direction === "local_to_cloud" ? "active" : ""} onClick={() => resetDirection("local_to_cloud")} type="button"><CloudUpload size={16} />Local to Cloud</button>
          <button className={direction === "cloud_to_local" ? "active" : ""} onClick={() => resetDirection("cloud_to_local")} type="button"><Download size={16} />Cloud to device</button>
        </div>
      </section>

      <ol className="brain-sync-steps" aria-label="Brain Sync progress">
        {["Eligibility", "Preflight", "Review", direction === "local_to_cloud" ? "Upload" : "Restore", "Validate", "Receipt"].map((label, index) => <li className={stage > index ? "active" : ""} key={label}><span>{index + 1}</span>{label}</li>)}
      </ol>

      <section className="brain-sync-workspace">
        <div className="brain-sync-visual">
          <BrainCanvas activeBrainId={activeBrainId} activity={{ active: ["preflight", "applying", "checking"].includes(phase), detail: phase.replace(/_/g, " "), label: direction === "local_to_cloud" ? "Cloud snapshot" : "Local restore", phase: phase === "preflight" ? "retrieving" : phase === "applying" || phase === "checking" ? "growing" : "idle" }} nodes={nodes} />
          <MetricGrid metrics={[
            { label: "Local brain", value: activeBrain ? brainName(activeBrain) : activeBrainId },
            { label: "Nodes", value: String(sourceNodeCount) },
            { label: "Edges", value: sourceEdgeCount ? String(sourceEdgeCount) : "measured at preflight" },
            { label: "Revision", value: sourceRevision ? String(sourceRevision) : "measured at preflight" },
          ]} />
        </div>

        <aside className="brain-sync-control" aria-live="polite">
          <div className="brain-sync-control-head"><ShieldCheck size={20} /><div><span>{connected ? "Detwin connected" : "Account connection"}</span><h3>{phase === "review" ? "Review before apply" : phase === "complete" ? "Sync receipt ready" : "Prepare verified snapshot"}</h3></div></div>

          {direction === "cloud_to_local" ? (
            <div className="brain-sync-fields">
              <label><span>Cloud brain</span><select value={cloudBrainId} onChange={(event) => setCloudBrainId(event.target.value)}><option value="">Connect to load Cloud brains</option>{cloudBrains.map((brain) => { const id = String(brain.brain_id || brain.id || ""); return <option key={id} value={id}>{String(brain.display_name || brain.name || id)}</option>; })}</select></label>
              <label><span>New local brain ID</span><input value={targetLocalId} onChange={(event) => setTargetLocalId(event.target.value)} /></label>
              <label><span>Display name</span><input value={targetDisplayName} onChange={(event) => setTargetDisplayName(event.target.value)} /></label>
              <label className="brain-sync-check"><input checked={overwriteExisting} onChange={(event) => setOverwriteExisting(event.target.checked)} type="checkbox" /><span>Replace an existing local brain only after validation and explicit Apply.</span></label>
            </div>
          ) : null}

          {phase === "review" ? (
            <div className="brain-sync-review">
              <MetricGrid metrics={[
                { label: "Direction", value: direction === "local_to_cloud" ? "Local to Cloud" : "Cloud to device" },
                { label: "Destination", value: direction === "local_to_cloud" ? String(binding?.destination_brain_id || "new Cloud brain") : targetLocalId },
                { label: "Snapshot", value: `${sourceNodeCount} nodes` },
                { label: "Cost", value: estimatedCredits === null ? "Platform preflight" : `${estimatedCredits} credits` },
              ]} />
              <label className="brain-sync-check"><input checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} type="checkbox" /><span>I reviewed the source, destination, scope and reported cost. Apply this exact snapshot.</span></label>
              <button className="primary brain-sync-primary" disabled={!confirmed} onClick={() => void applyReviewedSync()} type="button">{direction === "local_to_cloud" ? "Confirm upload and validate" : "Download, validate and restore"}<ArrowRight size={16} /></button>
            </div>
          ) : null}

          {["connecting", "preflight", "applying"].includes(phase) ? <Notice tone="pending" title="Operation in progress" detail={phase === "connecting" ? "Completing the secure Detwin account handoff." : phase === "preflight" ? "Measuring the real graph and requesting an authoritative preflight." : "Applying only the reviewed snapshot and preserving its receipt."} /> : null}
          {phase === "checking" ? <div className="brain-sync-review"><Receipt title="Original operation recorded" detail="Continue this operation; do not start a duplicate transfer." tone="active" /><button className="primary brain-sync-primary" onClick={() => void advanceCloudLifecycle()} type="button">{nextLabel}<RefreshCw size={16} /></button></div> : null}
          {phase === "complete" ? <div className="brain-sync-review"><Receipt title="Verified transfer complete" detail={String(receipt?.receipt_id || receipt?.operation_id || receipt?.status || "The final receipt was acknowledged.")} tone="ready" /><button className="secondary" onClick={() => resetDirection(direction)} type="button"><RefreshCw size={16} />Start another snapshot</button></div> : null}
          {phase === "error" ? <div className="brain-sync-review"><Notice tone="blocked" title="Brain Sync needs attention" detail={error} /><button className="secondary" onClick={() => void previewSync()} type="button"><RefreshCw size={16} />Run a fresh preflight</button></div> : null}
          {phase === "idle" ? <button className="primary brain-sync-primary" onClick={() => void previewSync()} type="button">{connected ? direction === "local_to_cloud" ? "Preview Cloud sync" : "Preview Cloud restore" : "Connect Detwin and continue"}<ArrowRight size={16} /></button> : null}
        </aside>
      </section>
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
    <div className="operation-grid health-workspace">
      <section className="command-surface health-command-surface">
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
      <div className="health-live-layout">
        <BrainCanvas activeBrainId={activeBrainId} activity={activity} nodes={nodes} variant="compact" />
        <HealthResultPanel result={result} />
      </div>
    </div>
  );
}

function HealthResultPanel({ result }: { result: Record<string, unknown> | null }) {
  if (!result) {
    return (
      <section className="result-panel">
        <PanelEyebrow icon={HeartPulse} label="Brain health" />
        <div className="empty-result compact"><HeartPulse size={22} /><strong>Health proof has not run</strong><span>Run the deterministic whole-brain scan to see coverage, score and prioritized repair paths.</span></div>
      </section>
    );
  }
  const data = resultData(result) || result;
  const report = objectAt(data, "brain_health_report") || objectAt(data, "health_report");
  const summary = objectAt(report, "summary");
  const alerts = arrayAt(report, "health_alerts").length ? arrayAt(report, "health_alerts") : arrayAt(data, "health_alerts");
  const score = Number(report?.overall_score || 0);
  const nodeCount = Number(summary?.node_count || 0);
  const edgeCount = Number(summary?.edge_count || 0);
  const readiness = String(report?.readiness || data.status || "complete").replace(/_/g, " ");
  return (
    <section className="result-panel health-result-panel">
      <PanelEyebrow icon={HeartPulse} label="Whole-brain verdict" />
      <div className="health-verdict">
        <div><span>Health score</span><strong>{score ? `${Math.round(score * 100)}%` : "Not scored"}</strong></div>
        <div><span>Analyzed</span><strong>{nodeCount} nodes</strong></div>
        <div><span>Connections</span><strong>{edgeCount} edges</strong></div>
        <div><span>Verdict</span><strong>{readiness}</strong></div>
      </div>
      <Notice
        detail={`The deterministic scan analyzed the persisted graph and returned ${alerts.length} prioritized ${alerts.length === 1 ? "issue" : "issues"}. It did not mutate memory.`}
        title={alerts.length ? "Review the priority repairs" : "No priority repair found"}
        tone={alerts.length ? "pending" : "ready"}
      />
      {alerts.length ? (
        <div className="health-alert-list">
          {alerts.slice(0, 4).map((item, index) => {
            const alert = isRecord(item) ? item : {};
            return (
              <article key={String(alert.alert_id || index)}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div><strong>{String(alert.signal_family || "Brain quality").replace(/_/g, " ")}</strong><p>{String(alert.product_gate_impact || alert.recommendation || "Review the technical evidence before applying a change.").replace(/_/g, " ")}</p></div>
                <em>{String(alert.recommendation || "review").replace(/_/g, " ")}</em>
              </article>
            );
          })}
        </div>
      ) : null}
      <details className="raw-receipt"><summary>Open exact health receipt</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>
    </section>
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
  const [providerKey, setProviderKey] = useState("");
  const [showProviderKey, setShowProviderKey] = useState(false);
  const [providerStatus, setProviderStatus] = useState<"checking" | "configured" | "required" | "failed">("checking");
  const [providerAction, setProviderAction] = useState<"testing" | "saving" | null>(null);
  const [providerNotice, setProviderNotice] = useState("");
  const [verifiedProviderKey, setVerifiedProviderKey] = useState("");

  const refreshProviderStatus = useCallback(async () => {
    setProviderStatus("checking");
    try {
      const payload = await readApi<Record<string, unknown>>("/setup/env");
      const provider = objectAt(payload, "provider");
      setProviderStatus(provider?.configured === true ? "configured" : "required");
    } catch {
      setProviderStatus("failed");
    }
  }, []);

  useEffect(() => {
    void refreshProviderStatus();
  }, [refreshProviderStatus]);

  const testProvider = async () => {
    const apiKey = providerKey.trim();
    if (!apiKey) return;
    setProviderAction("testing");
    setProviderNotice("");
    try {
      const response = await writeApi<Record<string, unknown>>("/setup/provider/test", { api_key: apiKey });
      const verified = response.ok === true;
      setVerifiedProviderKey(verified ? apiKey : "");
      setProviderNotice(verified ? "Connection verified. Save this exact key to enable local AI operations." : "The provider did not accept this key.");
    } catch (error) {
      setVerifiedProviderKey("");
      setProviderNotice(localRequestErrorDetail(errorMessage(error)));
    } finally {
      setProviderAction(null);
    }
  };

  const saveProvider = async () => {
    const apiKey = providerKey.trim();
    if (!apiKey) return;
    setProviderAction("saving");
    setProviderNotice("");
    try {
      await writeApi<Record<string, unknown>>("/setup/env", { agvm_llm_enabled: true, openai_api_key: apiKey });
      setProviderKey("");
      setVerifiedProviderKey("");
      setProviderNotice("Verified provider key saved server-side. AI actions will still fail closed if the provider later rejects a request.");
      await refreshProviderStatus();
    } catch (error) {
      setProviderNotice(localRequestErrorDetail(errorMessage(error)));
    } finally {
      setProviderAction(null);
    }
  };

  return (
    <div className="settings-grid">
      <Notice tone="ready" title="Local-first boundary" detail="This UI talks only to the local AGVM API configured with VITE_API_URL. It does not sign in, sync, bill, or unlock cloud modules." />
      <section className="settings-panel provider-setup-panel">
        <PanelEyebrow icon={KeyRound} label="AI provider" />
        <div className="provider-setup-heading">
          <div><h2>Connect local intelligence.</h2><p>Bootstrap, Grow and Context remain available as product surfaces, but AI-backed actions fail closed until a provider is configured.</p></div>
          <span className={`provider-status state-${providerStatus}`}>{providerStatus === "configured" ? "Configured" : providerStatus === "checking" ? "Checking" : providerStatus === "failed" ? "Unavailable" : "Setup required"}</span>
        </div>
        <label className="provider-key-field">
          <span>OpenAI API key</span>
          <div>
            <input
              aria-label="OpenAI API key"
              autoComplete="new-password"
              onChange={(event) => { setProviderKey(event.target.value); setVerifiedProviderKey(""); setProviderNotice(""); }}
              placeholder="Paste a key, test it, then save"
              type={showProviderKey ? "text" : "password"}
              value={providerKey}
            />
            <button aria-label={showProviderKey ? "Hide provider key" : "Show provider key"} className="icon-button" onClick={() => setShowProviderKey((current) => !current)} type="button">
              {showProviderKey ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>
        <p className="fine-print">The raw key is sent only to the local API, stored by its managed environment, and never written to browser storage or returned to the UI.</p>
        {providerNotice ? <p className="provider-setup-notice" role="status">{providerNotice}</p> : null}
        <div className="provider-setup-actions">
          <button className="secondary" disabled={!providerKey.trim() || providerAction !== null} onClick={() => void testProvider()} type="button">{providerAction === "testing" ? "Testing connection" : "Test connection"}</button>
          <button className="primary" disabled={!providerKey.trim() || verifiedProviderKey !== providerKey.trim() || providerAction !== null} onClick={() => void saveProvider()} type="button">{providerAction === "saving" ? "Saving key" : "Save verified key"}</button>
          <button className="secondary" disabled={providerAction !== null} onClick={() => void refreshProviderStatus()} type="button"><RefreshCw size={15} />Refresh status</button>
        </div>
      </section>
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
  const [selectedPoint, setSelectedPoint] = useState<BrainPoint3d | null>(null);
  const nodeLimit = density === "focus" ? 30 : density === "balanced" ? 90 : density === "detailed" ? 240 : nodes.length;
  const visibleNodes = nodes.slice(0, nodeLimit);
  const liveGraph = Boolean(activeBrainId && nodes.length);
  const [theme, setCanvasTheme] = useState<ThemeMode>(() => readTheme());
  useEffect(() => {
    const updateTheme = () => setCanvasTheme(readTheme());
    const observer = new MutationObserver(updateTheme);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-core-theme"] });
    updateTheme();
    return () => observer.disconnect();
  }, []);
  const points = useMemo(
    () => normalizeBrainPointCloud(visibleNodes.map((node, index) => nodePoint3d(node, index, visibleNodes.length))),
    [visibleNodes],
  );
  return (
    <section className={`brain-canvas ${variant} ${activity.active ? "is-active" : "is-idle"}`} aria-label="Local AGVM brain projection">
      <Canvas
        className="brain-three-canvas"
        camera={{ fov: variant === "stage" ? 42 : 48, position: [0, 0.16, variant === "stage" ? 5.2 : 5.8] }}
        dpr={[1, 1.35]}
        gl={{ antialias: true, powerPreference: "high-performance" }}
        onCreated={({ gl }) => {
          // R3F forces a WebGL loss event while unmounting a route. Dispose resources
          // without emitting a false GPU-loss signal; the detached canvas is then GC'd.
          gl.forceContextLoss = () => { gl.dispose(); };
        }}
      >
        <color args={[theme === "dark" ? "#071311" : "#f7faf9"]} attach="background" />
        <ambientLight intensity={0.68} />
        <directionalLight color="#f7fffb" intensity={1.2} position={[3.2, 4.5, 5]} />
        <pointLight color="#00e9b1" intensity={2.4} position={[-2.6, 1.8, 2.4]} />
        <pointLight color="#8b55e7" intensity={1.25} position={[2.8, -1.2, 2.2]} />
        <BrainThreeScene activity={activity} onSelectPoint={setSelectedPoint} points={points} variant={variant} />
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
      {selectedPoint ? (
        <aside className="brain-node-inspector" aria-live="polite">
          <button aria-label="Close node details" className="icon-button" onClick={() => setSelectedPoint(null)} title="Close node details" type="button"><X size={15} /></button>
          <span>{selectedPoint.memoryType}</span>
          <strong>{selectedPoint.label}</strong>
          <code>{selectedPoint.id}</code>
        </aside>
      ) : null}
    </section>
  );
}

function BrainThreeScene({
  activity,
  onSelectPoint,
  points,
  variant,
}: {
  activity: BrainActivity;
  onSelectPoint: (point: BrainPoint3d) => void;
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
      {pathPoints.length > 1 ? (
        <group>
          {pathPoints.slice(1).map((point, index) => {
            const previous = pathPoints[index];
            return <ConnectionTube active={active} from={previous.position} key={`${previous.id}-${point.id}`} to={point.position} />;
          })}
        </group>
      ) : null}

      {points.map((point, index) => (
        <MemoryNodeMesh active={active} key={`${point.id}-${index}`} onSelect={onSelectPoint} point={point} pulseOffset={index * 0.137} />
      ))}
    </group>
  );
}

function MemoryNodeMesh({ active, onSelect, point, pulseOffset }: { active: boolean; onSelect: (point: BrainPoint3d) => void; point: BrainPoint3d; pulseOffset: number }) {
  const meshRef = useRef<Group>(null);
  useFrame(({ clock }) => {
    if (!meshRef.current) return;
    const pulse = 1 + Math.sin(clock.getElapsedTime() * (active ? 3.2 : 1.4) + pulseOffset) * (active ? 0.22 : 0.08);
    meshRef.current.scale.setScalar(pulse);
  });
  return (
    <group
      onClick={() => onSelect(point)}
      onPointerOut={() => { document.body.style.cursor = ""; }}
      onPointerOver={() => { document.body.style.cursor = "pointer"; }}
      ref={meshRef}
      position={point.position}
    >
      <mesh>
        <sphereGeometry args={[point.size, 16, 10]} />
        <meshStandardMaterial color={point.color} emissive={point.color} emissiveIntensity={active ? 0.55 : 0.24} roughness={0.48} />
      </mesh>
      <mesh scale={2.15}>
        <sphereGeometry args={[point.size, 12, 8]} />
        <meshBasicMaterial color={point.color} opacity={0.11} transparent />
      </mesh>
      <Html center distanceFactor={7} zIndexRange={[12, 0]}>
        <button
          aria-label={`Open memory node ${point.label}`}
          className="brain-node-hitbox"
          onClick={(event) => {
            event.stopPropagation();
            onSelect(point);
          }}
          title={point.label}
          type="button"
        />
      </Html>
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
  const manageMenuRef = useRef<HTMLDetailsElement>(null);
  const closeManageMenu = () => {
    if (manageMenuRef.current) manageMenuRef.current.open = false;
  };
  return (
    <section className="brain-selector brain-management" title="Active local brain">
      <label>
        <Brain size={15} />
        <span>Active brain</span>
        <select disabled={!brains.length || busyAction === "select-brain"} onChange={(event) => onSelect(event.target.value)} value={activeBrainId || brains[0]?.brain_id || ""}>
          {brains.length ? brains.map((brain) => <option key={brainId(brain)} value={brainId(brain)}>{brainName(brain)}</option>) : <option value="">No local brain</option>}
        </select>
      </label>
      <details className="brain-actions-menu" ref={manageMenuRef}>
        <summary>Manage</summary>
        <div className="brain-menu-panel">
          <fieldset>
            <legend>Create local brain</legend>
            <input aria-label="New brain display name" onChange={(event) => setNewBrainDisplayName(event.target.value)} placeholder="My product brain" value={newBrainDisplayName} />
            <input aria-label="New brain id" onChange={(event) => setNewBrainId(event.target.value)} placeholder="Optional technical id" value={newBrainId} />
            <button disabled={busy || !newBrainDisplayName.trim()} onClick={() => { closeManageMenu(); onCreateBrain(); }} type="button"><PlusCircle size={15} />Create and select</button>
          </fieldset>
          <fieldset>
            <legend>Import brain archive</legend>
            <input aria-label="Imported brain display name" onChange={(event) => setImportBrainDisplayName(event.target.value)} placeholder="Name shown after import" value={importBrainDisplayName} />
            <input aria-label="Imported brain id" onChange={(event) => setImportBrainId(event.target.value)} placeholder="Optional technical id" value={importBrainId} />
            <label className="file-action">
              <FileUp size={15} />
              Import .zip
              <input accept=".zip,.agvm-brain,.agvm-brain.zip" disabled={busy} onChange={(event) => { const file = event.currentTarget.files?.[0] || null; if (file) closeManageMenu(); onImportFile(file); }} type="file" />
            </label>
          </fieldset>
          <div className="brain-menu-actions">
            <button disabled={busy} onClick={() => { closeManageMenu(); onBootstrap(); }} type="button"><RefreshCw size={15} />Refresh brain list</button>
            <button disabled={busy || !activeBrainId} onClick={() => { closeManageMenu(); onExportBrain(); }} type="button"><Download size={15} />Export active</button>
            <button disabled={busy} onClick={() => { closeManageMenu(); onRefresh(); }} type="button"><RefreshCw size={15} />Refresh</button>
            <a href="#brain_explorer" onClick={closeManageMenu}><Brain size={15} />Open Brain Explorer</a>
          </div>
        </div>
      </details>
    </section>
  );
}

function BrainBootstrapNotice({ busyAction, onBootstrap }: { busyAction: string | null; onBootstrap: () => void }) {
  const busy = Boolean(busyAction);
  return (
    <article className="bootstrap-notice">
      <Brain size={22} />
      <div>
        <strong>Create or import a local brain to start.</strong>
        <p>Context, Grow and MCP are brain-scoped. Use Manage in the top bar to name a new brain or import a reviewed archive.</p>
      </div>
      <button className="secondary" disabled={busy} onClick={onBootstrap} type="button"><RefreshCw size={16} />Refresh brain list</button>
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

function Notice({ actionLabel, detail, onAction, title, tone }: { actionLabel?: string; detail: string; onAction?: () => void; title: string; tone: "ready" | "blocked" | "pending" }) {
  const Icon = tone === "ready" ? CheckCircle2 : tone === "blocked" ? CircleAlert : RefreshCw;
  return (
    <article className={`notice ${tone}`}>
      <Icon size={18} />
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
      {actionLabel && onAction ? <button className="secondary" onClick={onAction} type="button">{actionLabel}<ArrowRight size={15} /></button> : null}
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
  const contextPackage = resultContextPackage(result);
  const sections = resultSectionSummaries(contextPackage);
  const evidence = resultEvidenceSummaries(contextPackage);
  const paths = resultPathSummaries(result);
  const answer = resultAnswer(result);
  const searchId = String(result?.search_id || "").trim();
  return (
    <section className="result-panel">
      <PanelEyebrow icon={MessageSquareText} label={contextPackage ? "Context package" : "Operation receipt"} />
      {result ? (
        <div className="structured-result">
          <div className="structured-result-head">
            <div><span>Status</span><strong>{resultStatusLabel(result)}</strong></div>
            {searchId ? <div><span>Search</span><strong>{searchId.slice(0, 8)}</strong></div> : null}
            <div><span>Evidence</span><strong>{evidence.length}</strong></div>
            {paths.length ? <div><span>Paths</span><strong>{paths.length}</strong></div> : null}
          </div>
          {answer ? <article className="result-answer"><span>Draft answer</span><p>{answer}</p></article> : null}
          {sections.length ? (
            <div className="result-sections">
              {sections.map((section) => (
                <article key={section.key}>
                  <div><strong>{section.title}</strong><span>{section.confidence}</span></div>
                  <ul>{section.items.map((item, index) => <li key={`${section.key}-${index}`}>{item}</li>)}</ul>
                </article>
              ))}
            </div>
          ) : null}
          {paths.length ? (
            <div className="result-path-list">
              <span className="result-subhead">Search paths</span>
              {paths.map((path, index) => (
                <article key={path.id || `path-${index + 1}`}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <div><strong>{path.title}</strong><p>{path.detail}</p></div>
                  <em>{path.status}</em>
                </article>
              ))}
            </div>
          ) : null}
          {evidence.length ? (
            <div className="result-evidence-list">
              {evidence.map((item, index) => (
                <details key={item.id || `${item.title}-${index}`}>
                  <summary><span>{String(index + 1).padStart(2, "0")}</span><strong>{item.title}</strong><em>{item.score}</em></summary>
                  <p>{item.summary}</p>
                  <div className="evidence-meta"><span>{item.lane}</span>{item.id ? <code>{item.id}</code> : null}</div>
                  {item.hydration ? <code className="hydration-recipe">Hydrate with {item.hydration}</code> : null}
                </details>
              ))}
            </div>
          ) : null}
          {!sections.length && !evidence.length && !answer ? <OperationSummary result={result} /> : null}
          <details className="raw-receipt">
            <summary>Technical receipt</summary>
            <pre>{JSON.stringify(result, null, 2)}</pre>
          </details>
        </div>
      ) : (
        <div className="empty-result">
          <Network size={26} />
          <strong>{emptyTitle}</strong>
          <span>A successful call will show readable context, evidence and a collapsible technical receipt here.</span>
        </div>
      )}
    </section>
  );
}

function OperationSummary({ result }: { result: Record<string, unknown> }) {
  const data = resultData(result) || result;
  const rows = Object.entries(data)
    .filter(([, value]) => typeof value === "string" || typeof value === "number" || typeof value === "boolean")
    .slice(0, 8);
  return rows.length ? (
    <dl className="operation-summary">
      {rows.map(([key, value]) => <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd>{String(value)}</dd></div>)}
    </dl>
  ) : <p className="fine-print">The operation completed. Open the technical receipt for its exact contract.</p>;
}

function GrowResultPanel({
  busy,
  emptyTitle,
  onApply,
  result,
}: {
  busy: boolean;
  emptyTitle: string;
  onApply: (selectedPreviewIds: string[]) => Promise<Record<string, unknown> | null | undefined>;
  result: Record<string, unknown> | null;
}) {
  const summary = growPreviewSummary(result);
  const candidates = growCandidateSummaries(result);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const candidateKey = candidates.map((candidate) => candidate.id).join("|");
  useEffect(() => {
    setSelectedIds(candidates.map((candidate) => candidate.id).filter(Boolean));
  }, [candidateKey]);
  return (
    <section className="result-panel grow-result-panel">
      <PanelEyebrow icon={GitBranch} label="Grow preview" />
      {result ? (
        <>
          <div className="grow-result-kpis">
            <Receipt title="Source units" detail={summary.sourceUnits} tone={summary.sourceUnits === "0" ? "pending" : "ready"} />
            <Receipt title="Candidate nodes" detail={summary.candidates} tone={summary.candidates === "0" ? "pending" : "active"} />
            {summary.applyState === "applied" ? <Receipt title="Graph delta" detail={`+${summary.graphDelta} nodes`} tone="ready" /> : null}
            <Receipt title="Write state" detail={summary.applyState} tone={summary.applyState === "review needed" ? "pending" : "ready"} />
          </div>
          {candidates.length ? (
            <div className="grow-candidate-list">
              {candidates.map((candidate, index) => (
                <label className={selectedIds.includes(candidate.id) ? "selected" : ""} key={candidate.id || `${candidate.title}-${index}`}>
                  <input checked={selectedIds.includes(candidate.id)} onChange={(event) => setSelectedIds((current) => event.target.checked ? [...new Set([...current, candidate.id])] : current.filter((id) => id !== candidate.id))} type="checkbox" />
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <div>
                    <strong>{candidate.title}</strong>
                    <p>{candidate.detail}</p>
                  </div>
                </label>
              ))}
            </div>
          ) : (
            <div className="empty-result compact">
              <Network size={22} />
              <strong>No candidate list returned yet</strong>
              <span>Run a source preview with enough source material to inspect proposed memory nodes before apply.</span>
              </div>
          )}
          {candidates.length && summary.applyState !== "applied" ? (
            <button className="primary wide" disabled={busy || !selectedIds.length} onClick={() => void onApply(selectedIds)} type="button">
              {busy ? <RefreshCw size={16} /> : <CheckCircle2 size={16} />}
              Apply {selectedIds.length} reviewed {selectedIds.length === 1 ? "memory" : "memories"}
            </button>
          ) : null}
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

function brainSyncPlatformOrigin() {
  const configuredOrigin = configuredBrainSyncPlatformOrigin();
  const explicit = new URLSearchParams(window.location.search).get("platform_url")?.trim() || "";
  if (!explicit) return configuredOrigin;
  try {
    const candidateOrigin = new URL(explicit).origin;
    return candidateOrigin === configuredOrigin ? candidateOrigin : configuredOrigin;
  } catch {
    return configuredOrigin;
  }
}

function configuredBrainSyncPlatformOrigin() {
  try {
    return new URL(cloudUrl).origin;
  } catch {
    return "https://app.detwin.ai";
  }
}

function connectedClientTokenForPlatform(platformOrigin: string) {
  const deviceToken = readLocalValue(connectedClientDeviceTokenKey);
  if (!deviceToken) return "";
  const boundOrigin = readLocalValue(connectedClientPlatformOriginKey);
  if (boundOrigin && boundOrigin !== platformOrigin) return "";
  return platformOrigin === configuredBrainSyncPlatformOrigin() ? deviceToken : "";
}

async function platformJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 120000);
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const platformOrigin = brainSyncPlatformOrigin();
  const deviceToken = connectedClientTokenForPlatform(platformOrigin);
  if (deviceToken) headers.set("X-AGVM-Device-Token", deviceToken);
  try {
    const response = await fetch(`${platformOrigin}${path.startsWith("/") ? path : `/${path}`}`, {
      ...init,
      credentials: "include",
      headers,
      signal: controller.signal,
    });
    const text = await response.text();
    let payload: unknown = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { detail: text };
      }
    }
    if (!response.ok) throw new Error(responseDetail(payload) || `Detwin returned HTTP ${response.status}.`);
    return payload as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

function connectedClientLoginUrl() {
  const deviceId = readOrCreateLocalValue("agvm.platform.connected_client.device_id.v1", "local-agvm");
  const fingerprint = readOrCreateLocalValue("agvm.platform.connected_client.fingerprint.v1", "sha256");
  const returnUrl = new URL(window.location.href);
  returnUrl.searchParams.delete("agvm_connected_client_handoff");
  returnUrl.searchParams.set("route", "brain_sync");
  returnUrl.hash = "brain_sync";
  const query = new URLSearchParams({
    device_label: "Local AGVM",
    intent: "deep_link",
    local_device_id: deviceId,
    machine_fingerprint_hash: fingerprint,
    next: "/account/brains/sync",
    return_url: returnUrl.toString(),
    source: "local_agvm",
  });
  return `${brainSyncPlatformOrigin()}/auth/login?${query.toString()}`;
}

async function exchangeConnectedClientHandoff(handoffToken: string) {
  const deviceId = readOrCreateLocalValue("agvm.platform.connected_client.device_id.v1", "local-agvm");
  const fingerprint = readOrCreateLocalValue("agvm.platform.connected_client.fingerprint.v1", "sha256");
  const response = await fetch(`${brainSyncPlatformOrigin()}/v1/account/connected-client/handoff/exchange`, {
    method: "POST",
    credentials: "omit",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      client_kind: "local_agvm",
      handoff_token: handoffToken,
      local_device_id: deviceId,
      machine_fingerprint_hash: fingerprint,
      return_origin: window.location.origin,
    }),
  });
  const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) throw new Error(responseDetail(payload) || `Detwin handoff returned HTTP ${response.status}.`);
  const deviceToken = String(payload.device_token || "").trim();
  if (!deviceToken) throw new Error("Detwin did not return a connected-device credential.");
  window.localStorage.setItem(connectedClientDeviceTokenKey, deviceToken);
  window.localStorage.setItem(connectedClientPlatformOriginKey, brainSyncPlatformOrigin());
}

function readLocalValue(key: string) {
  try {
    return String(window.localStorage.getItem(key) || "").trim();
  } catch {
    return "";
  }
}

function readOrCreateLocalValue(key: string, prefix: string) {
  const existing = readLocalValue(key);
  if (existing) return existing;
  const random = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  const value = `${prefix}:${random}`;
  window.localStorage.setItem(key, value);
  return value;
}

function durableOperationKey(scope: string) {
  const key = `agvm.operation.${scope}`;
  const existing = window.sessionStorage.getItem(key);
  if (existing) return existing;
  const random = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const value = `${scope}:${random}`.replace(/[^A-Za-z0-9:_.=-]/g, "_").slice(0, 128);
  window.sessionStorage.setItem(key, value);
  return value;
}

function brainSyncAccount(profile: Record<string, unknown>, summary: Record<string, unknown>): BrainSyncAccount {
  const organizationId = String(findNestedValue(summary, "organization_id") || findNestedValue(profile, "organization_id") || "");
  const workspaceId = String(findNestedValue(summary, "workspace_id") || findNestedValue(profile, "workspace_id") || "");
  const actorId = String(findNestedValue(profile, "user_id") || findNestedValue(profile, "actor_id") || findNestedValue(profile, "id") || "");
  if (!organizationId || !workspaceId || !actorId) throw new Error("Detwin did not return the active organization, workspace and user required for Brain Sync.");
  return { actorId, organizationId, workspaceId };
}

function findNestedValue(source: unknown, key: string): unknown {
  if (Array.isArray(source)) {
    for (const item of source) {
      const value = findNestedValue(item, key);
      if (value !== undefined && value !== null && value !== "") return value;
    }
    return undefined;
  }
  if (!isRecord(source)) return undefined;
  if (source[key] !== undefined && source[key] !== null && source[key] !== "") return source[key];
  for (const value of Object.values(source)) {
    const nested = findNestedValue(value, key);
    if (nested !== undefined && nested !== null && nested !== "") return nested;
  }
  return undefined;
}

function findNestedText(source: unknown, key: string) {
  return String(findNestedValue(source, key) || "").trim();
}

function findNestedNumber(source: unknown, ...keys: string[]) {
  for (const key of keys) {
    const value = findNestedValue(source, key);
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function snapshotHash(snapshot: Record<string, unknown>) {
  const text = JSON.stringify(snapshot);
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function cloudBrainIdFor(localBrainId: string) {
  const base = localBrainId.trim().toLowerCase().replace(/[^a-z0-9_.=-]+/g, "_").replace(/^[^a-z0-9]+/, "").replace(/_+/g, "_").slice(0, 96) || "local_brain";
  return `cloud_${base}`.slice(0, 120);
}

function syncAuthorityHeaders(authority: string): HeadersInit | undefined {
  const value = authority.trim();
  return value ? { "X-AGVM-Brain-Sync-Operation-Authority": value } : undefined;
}

function lastSyncReceipt(operation: Record<string, unknown>) {
  const direct = objectAt(operation, "receipt");
  if (direct) return direct;
  const receipts = arrayAt(operation, "receipts").filter(isRecord);
  return receipts.length ? receipts[receipts.length - 1] : null;
}

function isTerminalSync(operation: Record<string, unknown>) {
  const state = String(operation.state || operation.status || "").toLowerCase();
  return ["acknowledged", "complete", "completed", "ready", "synced"].includes(state)
    || findNestedValue(operation, "sync_claim_allowed") === true;
}

function syncErrorMessage(error: unknown) {
  const raw = errorMessage(error);
  const normalized = raw.toLowerCase();
  if (normalized.includes("auth") || normalized.includes("session") || normalized.includes("device")) return "Reconnect Detwin and run a new preflight. No snapshot was applied.";
  if (normalized.includes("credit") || normalized.includes("quota") || normalized.includes("insufficient")) return "The current credit allowance cannot cover this operation. Nothing was applied.";
  if (normalized.includes("revision") || normalized.includes("stale")) return "The source changed after preflight. Run a new preflight against the current revision.";
  if (normalized.includes("authority")) return "The verified sync operation expired. Run a new preflight before applying.";
  return raw.replace(/brain_sync_/gi, "").replace(/_/g, " ");
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

function activityFor(busyAction: string | null, route: RouteId, result: Record<string, unknown> | null = null): BrainActivity {
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
  if (route === "context") {
    const evidenceCount = resultEvidenceSummaries(resultContextPackage(result)).length;
    return evidenceCount
      ? { active: false, detail: `${evidenceCount} evidence ready`, label: "Context complete", phase: "idle" }
      : { active: false, detail: "ready for retrieval", label: "Context path", phase: "idle" };
  }
  if (route === "grow") {
    const preview = growPreviewSummary(result);
    return preview.applyState === "applied"
      ? { active: false, detail: `${preview.graphDelta} persisted nodes`, label: "Growth applied", phase: "idle" }
      : { active: false, detail: "preview required", label: "Growth path", phase: "idle" };
  }
  if (route === "mcp") return { active: false, detail: "raw catalog ready", label: "MCP path", phase: "idle" };
  if (route === "health") return { active: false, detail: "proof idle", label: "Health path", phase: "idle" };
  return { active: false, detail: "radial memory map", label: "Shape lock", phase: "idle" };
}

function headlineForRoute(route: RouteId, activeBrain: BrainSummary | null) {
  if (route === "brain_center") return activeBrain ? brainName(activeBrain) : "Create your first brain.";
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
  if (route === "brain_center") return "Create, select and complete the reviewed Bootstrap that unlocks a local brain.";
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
  return "Configure server-side local provider custody, interface preferences and explicit Cloud handoff links from one place.";
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

function normalizeBrainPointCloud(points: BrainPoint3d[]) {
  if (points.length < 2) return points;
  const center = points.reduce<[number, number, number]>(
    (value, point) => [value[0] + point.position[0], value[1] + point.position[1], value[2] + point.position[2]],
    [0, 0, 0],
  ).map((value) => value / points.length) as [number, number, number];
  const extents = points.reduce<[number, number, number]>(
    (value, point) => [
      Math.max(value[0], Math.abs(point.position[0] - center[0])),
      Math.max(value[1], Math.abs(point.position[1] - center[1])),
      Math.max(value[2], Math.abs(point.position[2] - center[2])),
    ],
    [0, 0, 0],
  );
  const scale = Math.min(
    extents[0] > 0.01 ? 2.05 / extents[0] : Number.POSITIVE_INFINITY,
    extents[1] > 0.01 ? 1.12 / extents[1] : Number.POSITIVE_INFINITY,
    extents[2] > 0.01 ? 0.98 / extents[2] : Number.POSITIVE_INFINITY,
    5.5,
  );
  if (!Number.isFinite(scale) || scale <= 1) return points;
  return points.map((point) => ({
    ...point,
    position: [
      (point.position[0] - center[0]) * scale,
      (point.position[1] - center[1]) * scale,
      (point.position[2] - center[2]) * scale,
    ] as [number, number, number],
  }));
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
  const status = String(data?.status || result?.status || "").toLowerCase();
  const applyState = status === "applied" ? "applied" : status === "blocked" ? "blocked" : applyContract ? "review needed" : "preview first";
  const graphDelta = numberString(completeness?.persisted_node_count) || (applyState === "applied" ? candidates : "0");
  return { applyState, candidates, graphDelta, sourceUnits };
}

function growCandidateSummaries(result: Record<string, unknown> | null) {
  const data = resultData(result);
  const previewBundle = objectAt(data, "preview_bundle") || objectAt(data, "previewBundle");
  const rawCandidates = arrayAt(previewBundle, "derived_nodes") || arrayAt(previewBundle, "candidate_nodes") || [];
  const candidates = rawCandidates
    .map((item) => (item && typeof item === "object" ? (item as Record<string, unknown>) : null))
    .filter((item): item is Record<string, unknown> => Boolean(item))
    .slice(0, 64)
    .map((item, index) => ({
      id: String(item.preview_id || item.candidate_id || item.node_id || item.id || `candidate-${index + 1}`),
      title: String(item.summary || item.title || item.label || item.node_id || "Candidate memory node"),
      detail: String(item.memory_type || item.source_label || item.confidence || item.rationale || "Review this candidate before any apply step."),
    }));
  if (!candidates.length && previewBundle?.primary_node_preview && typeof previewBundle.primary_node_preview === "object") {
    const primary = previewBundle.primary_node_preview as Record<string, unknown>;
    candidates.push({
      id: String(primary.preview_id || primary.candidate_id || primary.node_id || primary.id || "primary-candidate"),
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

function isBootstrapReady(brain: BrainSummary | null) {
  if (!brain) return false;
  if (String(brain.lifecycle?.bootstrap_state || "").toLowerCase() === "applied") return true;
  const source = String(brain.migration_source || "").toLowerCase();
  const reviewedImport = ["import", "migrat", "restore", "sync"].some((marker) => source.includes(marker));
  return reviewedImport && Number(brain.node_count || 0) > 0 && brain.safe_for_mcp !== false;
}

function bootstrapSessionState(session: Record<string, unknown> | null) {
  if (!session) return "not_started";
  return String(session.lifecycle_state || session.state || session.status || "in_progress").toLowerCase();
}

function bootstrapCandidates(session: Record<string, unknown> | null) {
  const previewEnvelope = objectAt(session, "preview");
  const preview = objectAt(session, "preview_bundle") || objectAt(previewEnvelope, "preview_bundle") || previewEnvelope;
  const raw = [
    ...arrayAt(session, "review_candidates"),
    ...arrayAt(session, "candidates"),
    ...arrayAt(preview, "derived_nodes"),
    ...arrayAt(preview, "candidate_nodes"),
  ];
  const seen = new Set<string>();
  return raw.flatMap((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const row = item as Record<string, unknown>;
    const id = String(row.preview_id || row.candidate_id || row.node_id || row.id || `candidate-${index + 1}`);
    if (seen.has(id)) return [];
    seen.add(id);
    return [{
      id,
      title: String(row.summary || row.title || row.label || "Candidate memory"),
      detail: String(row.content || row.detail || row.rationale || row.memory_type || "Reviewed Bootstrap memory"),
    }];
  });
}

function bootstrapStepState(state: string, index: number) {
  const rank: Record<string, number> = {
    not_started: 0,
    purpose_ready: 1,
    interview_ready: 1,
    interviewing: 1,
    answers_ready: 2,
    sources_ready: 3,
    preview_ready: 4,
    review_ready: 4,
    applied: 5,
  };
  const current = rank[state] ?? (state.includes("preview") ? 4 : state.includes("source") ? 3 : state.includes("answer") ? 2 : 1);
  return index < current ? "done" : index === current ? "active" : "pending";
}

function manualQuestionList(value: string) {
  return value.split(/\r?\n/).map((question) => question.trim()).filter(Boolean);
}

function bootstrapQuestionId(question: string, index: number) {
  return `q${index + 1}-${question.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 42)}`;
}

function objectAtValue(source: unknown, key: string) {
  if (!source || typeof source !== "object" || Array.isArray(source)) return "";
  return String((source as Record<string, unknown>)[key] || "");
}

function resultContextPackage(result: Record<string, unknown> | null) {
  const data = resultData(result);
  if (!data) return null;
  return objectAt(data, "context_package") || (String(data.schema_version || "").includes("context_package") ? data : null);
}

function resultSectionSummaries(contextPackage: Record<string, unknown> | null) {
  const rows = arrayAt(contextPackage, "structured_sections").length
    ? arrayAt(contextPackage, "structured_sections")
    : arrayAt(contextPackage, "sections");
  return rows.flatMap((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const row = item as Record<string, unknown>;
    const items = arrayAt(row, "items").map((value) => String(value || "").trim()).filter(Boolean);
    if (!items.length) return [];
    const confidence = typeof row.confidence === "number" ? `${Math.round(row.confidence * 100)}%` : "";
    return [{ key: String(row.key || `section-${index + 1}`), title: String(row.title || row.key || "Context"), items, confidence }];
  });
}

function resultEvidenceSummaries(contextPackage: Record<string, unknown> | null) {
  return arrayAt(contextPackage, "evidence").flatMap((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const row = item as Record<string, unknown>;
    const inspect = objectAt(row, "inspect");
    const score = typeof row.score === "number" ? `${Math.round(row.score * 100)}%` : "";
    const summary = String(row.summary || row.content || "Open the technical receipt for this evidence contract.");
    return [{
      id: String(row.node_id || row.document_id || row.id || ""),
      lane: String(row.lane || row.type || "memory"),
      title: summary,
      summary: String(row.content || row.source_excerpt || summary),
      score,
      hydration: String(inspect?.tool_name || inspect?.endpoint || ""),
    }];
  });
}

function resultPathSummaries(result: Record<string, unknown> | null) {
  const trace = objectAt(result, "ui_trace");
  return arrayAt(trace, "landing_metadata").flatMap((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const row = item as Record<string, unknown>;
    const studied = Number(row.studied_node_count || 0);
    const hydrated = Number(row.hydrated_node_count || 0);
    const routeEvents = Number(row.route_trace_count || 0);
    return [{
      id: String(row.landing_id || row.branch_id || `path-${index + 1}`),
      title: String(row.query_text || row.goal || row.label || `Search path ${index + 1}`),
      detail: `${studied} studied / ${hydrated} hydrated / ${routeEvents} route events`,
      status: pathStatusLabel(row),
    }];
  });
}

function pathStatusLabel(path: Record<string, unknown>) {
  const stopReason = String(path.stop_reason || "").trim().toLowerCase();
  if (stopReason === "budget_exhausted" && Number(path.hydrated_node_count || 0) > 0) return "completed at budget";
  return String(path.status || path.route_state || stopReason || "complete").replace(/_/g, " ");
}

function resultAnswer(result: Record<string, unknown> | null) {
  const data = resultData(result);
  const materialization = objectAt(data, "answer_demo_materialization") || objectAt(data, "answer_demo");
  const contextPackage = resultContextPackage(result);
  return String(
    materialization?.answer_markdown || materialization?.answer || materialization?.text ||
    contextPackage?.answer_markdown || contextPackage?.answer || "",
  ).trim();
}

function contextSearchId(result: Record<string, unknown>) {
  const data = resultData(result) || result;
  const payload = objectAt(data, "result");
  const contextPackage = objectAt(data, "context_package");
  const delivery = objectAt(data, "mcp_delivery_contract");
  const completion = objectAt(delivery, "completion_contract");
  const inspection = objectAt(completion, "inspection");
  const argumentsPayload = objectAt(inspection, "arguments");
  return String(
    data.search_id || payload?.search_id || contextPackage?.search_id || argumentsPayload?.search_id || "",
  ).trim();
}

function contextResultIsTerminal(result: Record<string, unknown>) {
  const data = resultData(result) || result;
  const payload = objectAt(data, "result");
  const delivery = objectAt(data, "mcp_delivery_contract");
  const terminal = data.result_ready_terminal ?? payload?.result_ready_terminal ?? delivery?.terminal_for_client;
  const pending = data.final_materialization_pending ?? payload?.final_materialization_pending ?? delivery?.final_materialization_pending;
  return terminal === true && pending !== true;
}

function waitFor(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

function resultStatusLabel(result: Record<string, unknown>) {
  const data = resultData(result) || result;
  const delivery = objectAt(data, "mcp_delivery_contract");
  const lifecycle = objectAt(data, "run_lifecycle_contract");
  const state = String(delivery?.client_payload_state || lifecycle?.terminal_state || data.status || result.status || "complete");
  if (state === "partial_context" || state === "background_running") return "Context ready, refining";
  if (state === "complete_context" || state === "contract_satisfied" || state === "sealed") return "Complete context";
  if (state === "applied") return "Applied";
  return state.replace(/_/g, " ");
}

function providerBlockReason(result: Record<string, unknown> | null) {
  const reason = resultBlockedReason(result);
  return reason.includes("provider") || reason.includes("llm") || reason.includes("ai_unavailable") ? reason : "";
}

function growSourceEvidenceBlock(result: Record<string, unknown> | null) {
  const data = resultData(result);
  const investigation = objectAt(data, "source_investigation");
  const formation = objectAt(data, "source_formation_contract");
  return [resultBlockedReason(result), investigation?.status, formation?.blocked_reason]
    .some((value) => String(value || "").toLowerCase().includes("rich_extraction_required"));
}

function resultBlockedReason(result: Record<string, unknown> | null) {
  const data = resultData(result);
  if (!data || String(data.status || result?.status || "").toLowerCase() !== "blocked") return "";
  const semantic = objectAt(data, "semantic_contract_runtime");
  const admission = objectAt(semantic, "search_ai_admission");
  const lifecycle = objectAt(data, "memory_operation_lifecycle_contract");
  const completeness = objectAt(data, "completeness");
  return String(
    admission?.reason || admission?.provider_error || semantic?.provider_state || semantic?.provider_error
      || lifecycle?.blocked_reason || completeness?.reason || "",
  ).toLowerCase();
}

function objectAt(source: unknown, key: string): Record<string, unknown> | null {
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const value = (source as Record<string, unknown>)[key];
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function arrayAt(source: unknown, key: string): unknown[] {
  if (!source || typeof source !== "object" || Array.isArray(source)) return [];
  const value = (source as Record<string, unknown>)[key];
  return Array.isArray(value) ? value : [];
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

function isProviderConfigurationError(error: string) {
  const normalized = error.toLowerCase();
  return normalized.includes("bootstrap_question_generation_unavailable")
    || normalized.includes("missing_api_key")
    || normalized.includes("provider_unavailable")
    || normalized.includes("provider key");
}

function localRequestErrorDetail(error: string) {
  if (isProviderConfigurationError(error)) return "Add and verify your provider key in Local Settings, then retry. The brain was not changed and no false AI result was created.";
  return error.replace(/_/g, " ");
}

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}
