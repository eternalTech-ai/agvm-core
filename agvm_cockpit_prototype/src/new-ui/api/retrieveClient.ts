import { API_BASE_URL, fetchJson } from "../../api/client";
import type {
  MissionDocumentRef,
  MissionHydratedDocument,
  MissionMode,
  MissionRefsPolicy,
  MissionRequestPlan,
  MissionTool,
} from "../mission/missionProjection";

type BuildRetrieveRequestPlanInput = {
  activeBrainId?: string | null;
  completePaths: boolean;
  includeAnswerDemo: boolean;
  mode: MissionMode;
  queryText: string;
  refsPolicy: MissionRefsPolicy;
  tool: MissionTool;
};

export type McpRetrieveResponse = Record<string, unknown>;
export type McpQueryResultResponse = Record<string, unknown>;
export type McpInspectContextPackageResponse = Record<string, unknown>;
export type McpInspectPathCorridorResponse = Record<string, unknown>;
export type McpRetrieveDocumentResponse = Record<string, unknown>;
export type McpQueryPlanResponse = Record<string, unknown> & {
  search_id?: string;
  brain_id?: string | null;
};
export type McpQueryRunResponse = {
  search_id: string;
  brain_id?: string | null;
  status: "created" | "running" | "completed" | "failed";
  stream_url: string;
  result_url: string;
};
export type McpStreamEvent = {
  seq?: number;
  event_type?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
};
export type RealtimeRetrieveCallbacks = {
  onPlan?: (plan: McpQueryPlanResponse) => void;
  onRun?: (run: McpQueryRunResponse) => void;
  onStreamEvent?: (event: McpStreamEvent) => void;
};

export function buildRetrieveRequestPlan(input: BuildRetrieveRequestPlanInput): MissionRequestPlan {
  return buildToolRequestPlan({ ...input, tool: normalizePrimaryTool(input.tool) });
}

export function buildFollowUpRequestPlan(input: BuildRetrieveRequestPlanInput): MissionRequestPlan {
  return buildToolRequestPlan(input);
}

function buildToolRequestPlan(input: BuildRetrieveRequestPlanInput): MissionRequestPlan {
  const queryText = input.queryText.trim();
  const tool = input.tool;
  const body: MissionRequestPlan["body"] = {
    query_text: queryText,
    retrieval_mode: input.mode,
    document_text_policy: input.refsPolicy,
    max_matches: maxMatchesForMode(input.mode),
    include_raw_text: input.refsPolicy !== "refs_only",
    include_answer_demo: input.includeAnswerDemo,
    complete_paths: input.completePaths || tool === "retrieve_path_corridor",
  };
  const brainId = String(input.activeBrainId || "").trim();
  if (brainId) body.brain_id = brainId;
  if (tool === "retrieve_document_workspace") body.context_package_mode = "broad_dossier";
  if (tool === "retrieve_path_corridor" || tool === "retrieve_source_trace") body.context_package_mode = "forensic_trace";

  return {
    schemaVersion: "agvm.ui.retrieve_request_plan.v1",
    tool,
    endpoint: endpointForTool(tool),
    method: "POST",
    body,
    followUpTools: followUpToolsFor(tool),
    liveBindingState: "ready_not_executed",
    mutationAllowed: false,
  };
}

export async function runMcpRetrieve(plan: MissionRequestPlan) {
  return fetchJson<McpRetrieveResponse>(plan.endpoint, {
    body: JSON.stringify(plan.body),
    method: plan.method,
    timeoutMs: timeoutForPlan(plan),
  });
}

export async function runMcpRealtimeRetrieve(plan: MissionRequestPlan, callbacks: RealtimeRetrieveCallbacks = {}) {
  const planned = await createMcpQueryPlan(plan);
  callbacks.onPlan?.(planned);
  const searchId = String(planned.search_id || "").trim();
  if (!searchId) throw new Error("AGVM realtime retrieve did not return a search id");

  const run = await startMcpQueryRun(searchId, plan.body.brain_id);
  callbacks.onRun?.(run);
  const streamedResult = await readMcpQueryStream(run.stream_url, {
    onStreamEvent: callbacks.onStreamEvent,
    timeoutMs: timeoutForPlan(plan),
  });
  if (streamedResult) {
    const resultSearchId = String(streamedResult.search_id || searchId).trim();
    try {
      return await inspectMcpContextPackage(resultSearchId, plan.body.brain_id);
    } catch {
      return streamedResult;
    }
  }
  return inspectMcpContextPackage(searchId, plan.body.brain_id);
}

export async function createMcpQueryPlan(plan: MissionRequestPlan) {
  return fetchJson<McpQueryPlanResponse>("/memory/query-plan", {
    body: JSON.stringify(queryPlanBodyFor(plan)),
    method: "POST",
    timeoutMs: Math.max(60000, Math.min(timeoutForPlan(plan), 90000)),
  });
}

export async function startMcpQueryRun(searchId: string, activeBrainId?: string | null) {
  const body: Record<string, unknown> = { search_id: searchId.trim() };
  const brainId = String(activeBrainId || "").trim();
  if (brainId) body.brain_id = brainId;
  return fetchJson<McpQueryRunResponse>("/memory/query-run", {
    body: JSON.stringify(body),
    method: "POST",
    timeoutMs: 20000,
  });
}

export async function fetchMcpQueryResult(searchId: string, endpoint?: string) {
  return fetchJson<McpQueryResultResponse>(queryResultEndpoint(searchId, endpoint), {
    method: "GET",
    timeoutMs: 120000,
  });
}

export async function inspectMcpContextPackage(searchId: string, activeBrainId?: string | null, options: { timeoutMs?: number } = {}) {
  const body: Record<string, unknown> = {
    search_id: searchId.trim(),
    include_debug: false,
    include_raw_text: false,
    include_answer_demo: false,
  };
  const brainId = String(activeBrainId || "").trim();
  if (brainId) body.brain_id = brainId;
  return fetchJson<McpInspectContextPackageResponse>("/mcp/inspect-context-package", {
    body: JSON.stringify(body),
    method: "POST",
    timeoutMs: options.timeoutMs ?? 120000,
  });
}

export async function inspectMcpPathCorridor(searchId: string, activeBrainId?: string | null) {
  const body: Record<string, unknown> = {
    search_id: searchId.trim(),
    include_debug: false,
    include_raw_text: false,
    include_answer_demo: false,
  };
  const brainId = String(activeBrainId || "").trim();
  if (brainId) body.brain_id = brainId;
  return fetchJson<McpInspectPathCorridorResponse>("/mcp/inspect-path-corridor", {
    body: JSON.stringify(body),
    method: "POST",
    timeoutMs: 240000,
  });
}

export async function retrieveMcpDocument(ref: MissionDocumentRef, activeBrainId?: string | null) {
  const body = { ...ref.hydrate.body };
  const brainId = String(activeBrainId || body.brain_id || "").trim();
  if (brainId) body.brain_id = brainId;
  const response = await fetchJson<McpRetrieveDocumentResponse>(ref.hydrate.endpoint, {
    body: JSON.stringify(body),
    method: "POST",
    timeoutMs: 120000,
  });
  return hydratedDocumentFromResponse(response, ref);
}

export function buildRetrieveRequestPlanFromResponse(response: McpQueryResultResponse, activeBrainId?: string | null): MissionRequestPlan {
  const root = response || {};
  const mode = normalizeMissionMode(root.retrieval_mode);
  const refsPolicy = normalizeRefsPolicy(root.document_text_policy);
  const queryText = queryTextFromResponse(root) || "reattached AGVM run";
  return buildRetrieveRequestPlan({
    activeBrainId: String(root.brain_id || activeBrainId || "").trim(),
    completePaths: Boolean(root.path_corridors && Array.isArray(root.path_corridors) && root.path_corridors.length),
    includeAnswerDemo: Boolean(root.answer_demo_materialization),
    mode,
    queryText,
    refsPolicy,
    tool: inferToolFromResult(root),
  });
}

function normalizePrimaryTool(tool: MissionTool): MissionTool {
  if (tool === "retrieve_document_workspace") return tool;
  return "retrieve_context";
}

function endpointForTool(tool: MissionTool) {
  if (tool === "retrieve_document_workspace") return "/mcp/retrieve-document-workspace";
  if (tool === "retrieve_path_corridor") return "/mcp/retrieve-path-corridor";
  if (tool === "retrieve_source_trace") return "/mcp/retrieve-source-trace";
  return "/mcp/retrieve-context";
}

function followUpToolsFor(tool: MissionTool): MissionTool[] {
  if (tool === "retrieve_document_workspace") return ["retrieve_source_trace"];
  return ["retrieve_path_corridor", "retrieve_source_trace", "retrieve_document_workspace"];
}

function queryResultEndpoint(searchId: string, endpoint?: string) {
  const normalizedEndpoint = String(endpoint || "").trim();
  if (normalizedEndpoint.startsWith("/memory/query-result/")) return normalizedEndpoint;
  return `/memory/query-result/${encodeURIComponent(searchId.trim())}`;
}

function maxMatchesForMode(mode: MissionMode) {
  if (mode === "flash") return 6;
  if (mode === "heavy") return 18;
  if (mode === "forensic") return 24;
  return 12;
}

function timeoutForMode(mode: MissionMode) {
  if (mode === "flash") return 60000;
  if (mode === "heavy") return 180000;
  if (mode === "forensic") return 240000;
  return 120000;
}

function timeoutForPlan(plan: MissionRequestPlan) {
  if (plan.tool === "retrieve_document_workspace") return Math.max(timeoutForMode(plan.body.retrieval_mode), 240000);
  if (plan.tool === "retrieve_path_corridor" || plan.tool === "retrieve_source_trace") return Math.max(timeoutForMode(plan.body.retrieval_mode), 180000);
  return timeoutForMode(plan.body.retrieval_mode);
}

function queryPlanBodyFor(plan: MissionRequestPlan) {
  const body: Record<string, unknown> = {
    brain_id: plan.body.brain_id,
    query_text: plan.body.query_text,
    thread_id: `agvm-ui-${Date.now()}`,
    mcp_tool_name: plan.tool,
    response_mode: plan.body.include_answer_demo ? "both" : "context",
    retrieval_mode: plan.body.retrieval_mode,
    context_package_mode: plan.body.context_package_mode || contextPackageModeForTool(plan.tool),
    document_text_policy: plan.body.document_text_policy,
    complete_paths: plan.body.complete_paths,
    max_matches: plan.body.max_matches,
  };
  return Object.fromEntries(Object.entries(body).filter(([, value]) => value !== undefined && value !== null && value !== ""));
}

function contextPackageModeForTool(tool: MissionTool) {
  if (tool === "retrieve_document_workspace") return "broad_dossier";
  if (tool === "retrieve_path_corridor" || tool === "retrieve_source_trace") return "forensic_trace";
  return "mcp_operational";
}

async function readMcpQueryStream(
  streamUrl: string,
  options: {
    onStreamEvent?: (event: McpStreamEvent) => void;
    timeoutMs: number;
  },
) {
  const normalizedUrl = streamUrl.startsWith("http") ? streamUrl : `${API_BASE_URL}${streamUrl.startsWith("/") ? streamUrl : `/${streamUrl}`}`;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), options.timeoutMs);
  try {
    const response = await fetch(normalizedUrl, {
      headers: { Accept: "text/event-stream" },
      signal: controller.signal,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(text || `AGVM stream failed: ${response.status}`);
    }
    if (!response.body) throw new Error("AGVM stream response has no body");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const messages = buffer.split(/\r?\n\r?\n/);
      buffer = messages.pop() || "";
      for (const message of messages) {
        const event = parseSseMessage(message);
        if (!event) continue;
        options.onStreamEvent?.(event);
        const payload = asRecord(event.payload);
        if (event.event_type === "search_failed") {
          throw new Error(firstText(payload.error, payload.detail, "AGVM search failed"));
        }
        if (event.event_type === "result_ready" && payload.result && typeof payload.result === "object") {
          return payload.result as McpRetrieveResponse;
        }
      }
    }
    const finalEvent = parseSseMessage(buffer);
    if (finalEvent) {
      options.onStreamEvent?.(finalEvent);
      const payload = asRecord(finalEvent.payload);
      if (finalEvent.event_type === "search_failed") {
        throw new Error(firstText(payload.error, payload.detail, "AGVM search failed"));
      }
      if (finalEvent.event_type === "result_ready" && payload.result && typeof payload.result === "object") {
        return payload.result as McpRetrieveResponse;
      }
    }
    return null;
  } finally {
    window.clearTimeout(timeout);
  }
}

function parseSseMessage(message: string): McpStreamEvent | null {
  const data = message
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n")
    .trim();
  if (!data) return null;
  try {
    const parsed = JSON.parse(data);
    return parsed && typeof parsed === "object" ? (parsed as McpStreamEvent) : null;
  } catch {
    return null;
  }
}

function normalizeMissionMode(value: unknown): MissionMode {
  if (value === "flash" || value === "balanced" || value === "heavy" || value === "forensic") return value;
  return "balanced";
}

function normalizeRefsPolicy(value: unknown): MissionRefsPolicy {
  if (value === "refs_only" || value === "top_raw" || value === "all_raw") return value;
  return "refs_only";
}

function inferToolFromResult(response: McpQueryResultResponse): MissionTool {
  const toolName = String(response.tool_name || response.mcp_tool_name || "").trim();
  if (toolName === "retrieve_document_workspace") return "retrieve_document_workspace";
  const delivery = response.mcp_delivery_contract && typeof response.mcp_delivery_contract === "object" ? (response.mcp_delivery_contract as Record<string, unknown>) : {};
  const effectiveTool = String(delivery.effective_delivery_tool_name || delivery.originating_tool_name || "").trim();
  if (effectiveTool === "retrieve_document_workspace") return "retrieve_document_workspace";
  if (response.document_workspace && typeof response.document_workspace === "object" && !response.context_package) return "retrieve_document_workspace";
  return "retrieve_context";
}

function queryTextFromResponse(response: McpQueryResultResponse) {
  const direct = String(response.query_text || response.user_query || "").trim();
  if (direct) return direct;
  const semanticContract = asRecord(response.semantic_contract);
  const semanticQuery = String(semanticContract.user_query || semanticContract.query_text || "").trim();
  if (semanticQuery) return semanticQuery;
  const contextPackage = asRecord(response.context_package);
  return queryTextFromAgentMarkdown(String(contextPackage.agent_markdown || ""));
}

function queryTextFromAgentMarkdown(markdown: string) {
  const normalized = markdown.replace(/\r\n/g, "\n");
  const match = normalized.match(/##\s*Task\s*\/\s*User Intent\s*\n+([^\n]+)/i);
  return String(match?.[1] || "").trim();
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function hydratedDocumentFromResponse(response: McpRetrieveDocumentResponse, ref: MissionDocumentRef): MissionHydratedDocument {
  const documentBundle = asRecord(response.document_bundle);
  const documentWorkspace = asRecord(response.document_workspace);
  const bundleDocs = recordArray(documentBundle.documents);
  const workspaceDocs = recordArray(documentWorkspace.documents);
  const document = firstDocumentForRef(ref, bundleDocs, workspaceDocs);
  const rawText = firstText(document.raw_text, document.full_text, document.anchor_raw_text, document.text);
  const rawTextCharCount = numberValue(document.raw_text_char_count, document.full_text_char_count, rawText.length) || rawText.length;
  const includedCount = numberValue(document.raw_text_included_char_count, document.full_text_included_char_count, document.included_char_count, rawText.length) || rawText.length;
  const delivery = asRecord(response.mcp_delivery_contract);
  return {
    documentId: firstText(document.document_id, document.anchor_node_id, response.document_id, ref.documentId) || ref.id,
    title: firstText(document.title, ref.title) || "Document",
    sourceLabel: firstText(document.source_label, ref.sourceLabel) || undefined,
    sourceType: firstText(document.source_type, ref.sourceType) || undefined,
    status: firstText(response.status, delivery.client_payload_state) || "unknown",
    terminalForClient: boolValue(delivery.terminal_for_client),
    rawText,
    rawTextCharCount,
    rawTextIncludedCharCount: includedCount,
    rawTextTruncated: boolLike(document.raw_text_truncated, document.full_text_truncated, document.truncated),
    fetchedAt: new Date().toISOString(),
  };
}

function firstDocumentForRef(ref: MissionDocumentRef, ...groups: Record<string, unknown>[][]) {
  const ids = new Set([ref.documentId, ref.anchorNodeId, ref.id].filter(Boolean));
  for (const group of groups) {
    const exact = group.find((doc) => ids.has(firstText(doc.document_id, doc.anchor_node_id, doc.id)));
    if (exact) return exact;
  }
  for (const group of groups) {
    if (group[0]) return group[0];
  }
  return {};
}

function recordArray(value: unknown) {
  return Array.isArray(value) ? value.map(asRecord).filter((record) => Object.keys(record).length) : [];
}

function firstText(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
    if (typeof value === "boolean") return String(value);
  }
  return "";
}

function numberValue(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
}

function boolValue(value: unknown): boolean | null {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (normalized === "true") return true;
    if (normalized === "false") return false;
  }
  return null;
}

function boolLike(...values: unknown[]) {
  for (const value of values) {
    const normalized = boolValue(value);
    if (normalized !== null) return normalized;
  }
  return false;
}
