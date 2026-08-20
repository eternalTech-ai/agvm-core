import {
  AlertTriangle,
  Braces,
  Clipboard,
  Code2,
  Copy,
  Database,
  FileJson,
  Loader2,
  Play,
  ShieldCheck,
  SquareTerminal,
  Terminal,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState, type ChangeEvent } from "react";

import {
  executeMcpRawTool,
  fetchMcpContractRegistry,
  groupMcpToolsByPermissionFamily,
  mcpRawConfirmationPhrase,
  permissionFamilyRequiresConfirmation,
  samplePayloadForMcpTool,
  type JsonSchemaObject,
  type McpContractRegistry,
  type McpPermissionFamily,
  type McpToolContract,
} from "../api/mcpRawConsoleClient";
import type { OpsWorkspaceContext } from "../ops/opsWorkspaceTypes";
import { ProductPageFrame, type ProductMetric } from "../shell/ProductPageFrame";

type RawExecutionState = {
  endpoint: string;
  error: string | null;
  responseText: string;
  status: "idle" | "loading" | "ready" | "failed";
  toolName: string;
};

type RecentRawCall = {
  endpoint: string;
  family: McpPermissionFamily;
  status: "ready" | "failed";
  toolName: string;
  timestamp: string;
};

const emptyExecution: RawExecutionState = {
  endpoint: "",
  error: null,
  responseText: "",
  status: "idle",
  toolName: "",
};

const familyCopy: Record<McpPermissionFamily, { label: string; detail: string; icon: LucideIcon }> = {
  read_only: {
    detail: "Reads memory or registry state without mutation.",
    icon: Database,
    label: "Read only",
  },
  read_only_export: {
    detail: "Exports or returns larger read-only packages.",
    icon: FileJson,
    label: "Read/export",
  },
  registry_write: {
    detail: "Creates or selects registry objects such as brains.",
    icon: Database,
    label: "Registry write",
  },
  preview_only: {
    detail: "Builds previews. It should not persist memory by itself.",
    icon: Braces,
    label: "Preview only",
  },
  explicit_apply: {
    detail: "Persists approved changes and requires an operator confirmation.",
    icon: ShieldCheck,
    label: "Explicit apply",
  },
  destructive: {
    detail: "Reserved for destructive operations. Keep blocked for normal clients.",
    icon: AlertTriangle,
    label: "Destructive",
  },
};

export function McpRawConsoleWorkspace({ context }: { context: OpsWorkspaceContext }) {
  const [registry, setRegistry] = useState<McpContractRegistry | null>(null);
  const [registryError, setRegistryError] = useState<string | null>(null);
  const [loadingRegistry, setLoadingRegistry] = useState(true);
  const [selectedToolName, setSelectedToolName] = useState("");
  const [payloadText, setPayloadText] = useState("{}");
  const [confirmationText, setConfirmationText] = useState("");
  const [execution, setExecution] = useState<RawExecutionState>(emptyExecution);
  const [recentCalls, setRecentCalls] = useState<RecentRawCall[]>([]);
  const [copyState, setCopyState] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadingRegistry(true);
    setRegistryError(null);
    fetchMcpContractRegistry()
      .then((payload) => {
        if (cancelled) return;
        setRegistry(payload);
        setSelectedToolName((current) => current || preferredInitialTool(payload));
      })
      .catch((error) => {
        if (cancelled) return;
        setRegistryError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (!cancelled) setLoadingRegistry(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const groupedTools = useMemo(() => groupMcpToolsByPermissionFamily(registry?.tools || []), [registry]);
  const selectedTool = useMemo(
    () => registry?.tools.find((tool) => tool.name === selectedToolName) || null,
    [registry, selectedToolName],
  );
  const activeBrainId = context.brainManagement.activeBrainId;
  const requiresConfirmation = selectedTool ? permissionFamilyRequiresConfirmation(selectedTool.permission_family) : false;
  const confirmationPhrase = selectedTool ? mcpRawConfirmationPhrase(selectedTool) : "";
  const canExecute =
    Boolean(selectedTool) &&
    execution.status !== "loading" &&
    (!requiresConfirmation || confirmationText.trim() === confirmationPhrase);

  useEffect(() => {
    if (!selectedTool) return;
    setPayloadText(formatJson(samplePayloadForMcpTool(selectedTool, activeBrainId)));
    setConfirmationText("");
    setExecution((current) => ({ ...current, error: null }));
  }, [activeBrainId, selectedTool]);

  const metrics: ProductMetric[] = [
    {
      label: "Contracts",
      value: loadingRegistry ? "loading" : `${registry?.tools.length || 0}`,
      detail: registry?.registry_status || "from /mcp/contracts",
    },
    {
      label: "Active brain",
      value: activeBrainId || "unset",
      detail: "used only when the selected MCP tool requires brain_id",
    },
    {
      label: "Safety",
      value: selectedTool ? familyCopy[selectedTool.permission_family].label : "none",
      detail: selectedTool ? selectedTool.scope_policy : "select a contract",
    },
  ];

  function selectTool(tool: McpToolContract) {
    setSelectedToolName(tool.name);
  }

  function onPayloadChange(event: ChangeEvent<HTMLTextAreaElement>) {
    setPayloadText(event.target.value);
  }

  async function executeSelectedTool() {
    if (!selectedTool) return;
    if (requiresConfirmation && confirmationText.trim() !== confirmationPhrase) {
      setExecution({
        endpoint: selectedTool.endpoint_path,
        error: `Type ${confirmationPhrase} before running this apply-capable tool.`,
        responseText: "",
        status: "failed",
        toolName: selectedTool.name,
      });
      return;
    }

    let payload: Record<string, unknown>;
    try {
      payload = parsePayload(payloadText);
    } catch (error) {
      setExecution({
        endpoint: selectedTool.endpoint_path,
        error: error instanceof Error ? error.message : String(error),
        responseText: "",
        status: "failed",
        toolName: selectedTool.name,
      });
      return;
    }

    setExecution({
      endpoint: selectedTool.endpoint_path,
      error: null,
      responseText: "",
      status: "loading",
      toolName: selectedTool.name,
    });
    try {
      const response = await executeMcpRawTool({
        endpointPath: selectedTool.endpoint_path,
        method: selectedTool.http_method,
        payload,
        toolName: selectedTool.name,
      });
      setExecution({
        endpoint: selectedTool.endpoint_path,
        error: null,
        responseText: formatJson(response),
        status: "ready",
        toolName: selectedTool.name,
      });
      rememberCall(selectedTool, "ready");
    } catch (error) {
      setExecution({
        endpoint: selectedTool.endpoint_path,
        error: error instanceof Error ? error.message : String(error),
        responseText: "",
        status: "failed",
        toolName: selectedTool.name,
      });
      rememberCall(selectedTool, "failed");
    }
  }

  function rememberCall(tool: McpToolContract, status: "ready" | "failed") {
    setRecentCalls((items) => [
      {
        endpoint: tool.endpoint_path,
        family: tool.permission_family,
        status,
        timestamp: new Date().toLocaleTimeString(),
        toolName: tool.name,
      },
      ...items,
    ].slice(0, 6));
  }

  async function copyText(id: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyState(id);
      window.setTimeout(() => setCopyState((current) => (current === id ? null : current)), 1400);
    } catch {
      setCopyState("copy_failed");
    }
  }

  const usagePrompt = selectedTool ? buildUsagePrompt(selectedTool, activeBrainId) : "";
  const requestReceipt = selectedTool ? buildRequestReceipt(selectedTool, payloadText, confirmationPhrase) : "";

  return (
    <ProductPageFrame
      actions={[]}
      className="mcp-raw-console-frame"
      eyebrow="Core MCP"
      icon={SquareTerminal}
      intent="Raw, contract-driven console for calling public-core AGVM MCP endpoints without mounting paid module UI."
      metrics={metrics}
      mode="mcp_raw_console"
      status={loadingRegistry ? "loading contracts" : registryError ? "contract error" : executionStatusLabel(execution)}
      title="MCP Raw Console"
    >
      <section className="settings-brain-workspace mcp-raw-console-workspace">
        <div className="settings-scope-hero">
          <div className="settings-scope-copy">
            <span>Operator flow</span>
            <h2>Select a tool, edit JSON, run through the MCP contract.</h2>
            <p>
              This page calls the same local backend endpoints advertised by the MCP registry. Apply-capable tools are
              locked behind a typed confirmation so the core UI cannot silently persist memory.
            </p>
          </div>
          <div className="settings-scope-map" aria-label="MCP raw console path">
            <i />
            <span>contract</span>
            <i />
            <span>json</span>
            <i />
            <span>receipt</span>
          </div>
        </div>

        {registryError ? (
          <RawConsoleNotice
            icon={AlertTriangle}
            title="Contract registry unavailable"
            body={registryError}
            tone="blocked"
          />
        ) : null}

        <div className="settings-main-grid mcp-raw-console-grid">
          <section className="settings-panel mcp-raw-tool-browser">
            <PanelHeader
              eyebrow="Contracts"
              title="Tool contracts"
              detail={loadingRegistry ? "Loading /mcp/contracts" : "Grouped by permission family."}
            />
            {loadingRegistry ? <InlineLoader label="Loading MCP contracts" /> : null}
            {groupedTools.map((group) => (
              <ToolFamilyGroup
                activeToolName={selectedToolName}
                group={group}
                key={group.family}
                onSelectTool={selectTool}
              />
            ))}
          </section>

          <section className="settings-panel mcp-raw-tool-detail">
            {selectedTool ? (
              <>
                <PanelHeader
                  eyebrow={selectedTool.permission_family}
                  title={selectedTool.title || selectedTool.name}
                  detail={`${selectedTool.http_method} ${selectedTool.endpoint_path}`}
                />
                <RawConsoleNotice
                  icon={familyCopy[selectedTool.permission_family].icon}
                  title={familyCopy[selectedTool.permission_family].label}
                  body={familyCopy[selectedTool.permission_family].detail}
                  tone={selectedTool.permission_family === "destructive" ? "blocked" : "ready"}
                />
                <DefinitionRows
                  rows={[
                    ["Tool", selectedTool.name],
                    ["Status", selectedTool.implementation_status],
                    ["Scope", selectedTool.scope_policy],
                    ["Brain ID", selectedTool.requires_brain_id ? "Required by contract" : "Not required"],
                    ["Use when", selectedTool.client_usage?.when_to_use || selectedTool.description],
                    ["Result", selectedTool.client_usage?.default_output_package || selectedTool.default_output_package],
                  ]}
                />
                <SchemaSummary schema={selectedTool.input_schema} />
                <div className="settings-form-grid mcp-raw-request-editor">
                  <label>
                    <span>Request JSON</span>
                    <textarea
                      aria-label="MCP request JSON"
                      onChange={onPayloadChange}
                      spellCheck={false}
                      value={payloadText}
                    />
                  </label>
                </div>
                {requiresConfirmation ? (
                  <label className="settings-check mcp-raw-confirmation">
                    <span>
                      <AlertTriangle size={15} />
                      Type <strong>{confirmationPhrase}</strong> to run this apply-capable tool.
                    </span>
                    <input
                      aria-label="Apply confirmation phrase"
                      onChange={(event) => setConfirmationText(event.target.value)}
                      placeholder={confirmationPhrase}
                      value={confirmationText}
                    />
                  </label>
                ) : null}
                <div className="settings-command-grid">
                  <button className="settings-primary-action" disabled={!canExecute} onClick={executeSelectedTool} type="button">
                    {execution.status === "loading" ? <Loader2 size={15} /> : <Play size={15} />}
                    <span>{execution.status === "loading" ? "Running" : "Run selected MCP tool"}</span>
                  </button>
                  <button onClick={() => setPayloadText(formatJson(samplePayloadForMcpTool(selectedTool, activeBrainId)))} type="button">
                    <Braces size={15} />
                    <span>Reset sample JSON</span>
                  </button>
                  <button onClick={() => copyText("request", requestReceipt)} type="button">
                    <Clipboard size={15} />
                    <span>{copyState === "request" ? "Copied" : "Copy request receipt"}</span>
                  </button>
                </div>
              </>
            ) : (
              <InlineLoader label="Select an MCP contract" />
            )}
          </section>

          <section className="settings-panel mcp-raw-response-panel">
            <PanelHeader
              eyebrow="Receipt"
              title="Raw response"
              detail={execution.toolName ? `${execution.toolName} via ${execution.endpoint}` : "Run a tool to view the backend response."}
            />
            {execution.error ? (
              <RawConsoleNotice icon={AlertTriangle} title="Execution failed" body={execution.error} tone="blocked" />
            ) : null}
            {execution.status === "loading" ? <InlineLoader label="Waiting for AGVM backend response" /> : null}
            <pre className="payload-preview mcp-raw-response" title="Raw JSON response">
              {execution.responseText || "No response yet."}
            </pre>
            <div className="settings-command-grid">
              <button disabled={!execution.responseText} onClick={() => copyText("response", execution.responseText)} type="button">
                <Copy size={15} />
                <span>{copyState === "response" ? "Copied" : "Copy response"}</span>
              </button>
              <button disabled={!usagePrompt} onClick={() => copyText("prompt", usagePrompt)} type="button">
                <Code2 size={15} />
                <span>{copyState === "prompt" ? "Copied" : "Copy AI prompt"}</span>
              </button>
            </div>
            <RecentCalls calls={recentCalls} />
          </section>
        </div>
      </section>
    </ProductPageFrame>
  );
}

function ToolFamilyGroup({
  activeToolName,
  group,
  onSelectTool,
}: {
  activeToolName: string;
  group: { family: McpPermissionFamily; tools: McpToolContract[] };
  onSelectTool: (tool: McpToolContract) => void;
}) {
  const copy = familyCopy[group.family];
  const Icon = copy.icon;
  return (
    <div className="settings-resolution-grid mcp-raw-family-group">
      <div className="settings-panel-head">
        <span>
          <Icon size={15} />
          {copy.label}
        </span>
        <p>{copy.detail}</p>
      </div>
      {group.tools.map((tool) => (
        <button
          className={activeToolName === tool.name ? "active" : ""}
          key={tool.name}
          onClick={() => onSelectTool(tool)}
          title={tool.description}
          type="button"
        >
          <strong>{tool.title || tool.name}</strong>
          <span>{tool.name}</span>
          <small>{tool.http_method} {tool.endpoint_path}</small>
        </button>
      ))}
    </div>
  );
}

function PanelHeader({ detail, eyebrow, title }: { detail: string; eyebrow: string; title: string }) {
  return (
    <header className="settings-panel-head">
      <span>{eyebrow}</span>
      <h2>{title}</h2>
      <p>{detail}</p>
    </header>
  );
}

function DefinitionRows({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="definition-rows">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd title={value}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function SchemaSummary({ schema }: { schema: JsonSchemaObject }) {
  const properties = Object.entries(schema.properties || {});
  const required = new Set(schema.required || []);
  return (
    <div className="settings-resolution-receipt">
      <span>
        <FileJson size={15} />
        Input schema
      </span>
      {properties.length ? (
        <ul className="evidence-list">
          {properties.map(([name, property]) => (
            <li key={name}>
              <strong>{name}{required.has(name) ? " *" : ""}</strong>
              <span>{schemaTypeLabel(property)}{property.description ? ` - ${property.description}` : ""}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p>No request body required by this contract.</p>
      )}
    </div>
  );
}

function RawConsoleNotice({
  body,
  icon: Icon,
  title,
  tone,
}: {
  body: string;
  icon: LucideIcon;
  title: string;
  tone: "ready" | "blocked";
}) {
  return (
    <article className={`product-state-card mcp-raw-notice mcp-raw-notice-${tone}`}>
      <span>
        <Icon size={15} />
        {title}
      </span>
      <strong>{body}</strong>
    </article>
  );
}

function InlineLoader({ label }: { label: string }) {
  return (
    <article className="product-state-card">
      <span>
        <Loader2 size={15} />
        {label}
      </span>
      <strong>working</strong>
    </article>
  );
}

function RecentCalls({ calls }: { calls: RecentRawCall[] }) {
  if (!calls.length) {
    return (
      <article className="product-state-card">
        <span>
          <Terminal size={15} />
          Recent calls
        </span>
        <strong>No calls in this browser session.</strong>
      </article>
    );
  }
  return (
    <div className="settings-resolution-receipt">
      <span>
        <Terminal size={15} />
        Recent calls
      </span>
      <ul className="evidence-list">
        {calls.map((call) => (
          <li key={`${call.timestamp}-${call.toolName}`}>
            <strong>{call.toolName}</strong>
            <span>{familyCopy[call.family].label} / {call.status} / {call.timestamp}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function preferredInitialTool(registry: McpContractRegistry) {
  return registry.tools.find((tool) => tool.name === "get_agvm_usage_guide")?.name || registry.tools[0]?.name || "";
}

function parsePayload(value: string): Record<string, unknown> {
  const parsed = JSON.parse(value || "{}") as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Request JSON must be an object.");
  }
  return parsed as Record<string, unknown>;
}

function formatJson(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function schemaTypeLabel(schema: JsonSchemaObject) {
  if (Array.isArray(schema.type)) return schema.type.join(" | ");
  return schema.type || "any";
}

function executionStatusLabel(execution: RawExecutionState) {
  if (execution.status === "loading") return "running";
  if (execution.status === "ready") return "response ready";
  if (execution.status === "failed") return "failed";
  return "ready";
}

function buildUsagePrompt(tool: McpToolContract, activeBrainId: string) {
  return [
    "Use AGVM through the MCP contract.",
    `First call get_agvm_usage_guide, then call ${tool.name} only if it fits the task.`,
    `Permission family: ${tool.permission_family}.`,
    `Endpoint: ${tool.http_method} ${tool.endpoint_path}.`,
    activeBrainId ? `Current UI brain_id hint: ${activeBrainId}.` : "No UI brain_id hint is selected.",
    tool.safety_contract?.requires_explicit_apply
      ? "For apply-capable work, create or inspect the preview first and ask the operator before persisting."
      : "Do not persist memory unless the contract explicitly requires an apply step.",
  ].join("\n");
}

function buildRequestReceipt(tool: McpToolContract, payloadText: string, confirmationPhrase: string) {
  return [
    `tool=${tool.name}`,
    `permission_family=${tool.permission_family}`,
    `endpoint=${tool.http_method} ${tool.endpoint_path}`,
    confirmationPhrase ? `confirmation_phrase=${confirmationPhrase}` : "confirmation_phrase=not_required",
    "payload=",
    payloadText,
  ].join("\n");
}
