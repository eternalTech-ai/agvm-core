import { fetchJson } from "../../api/client";

export type McpPermissionFamily =
  | "read_only"
  | "read_only_export"
  | "registry_write"
  | "preview_only"
  | "explicit_apply"
  | "destructive";

export type McpContractHttpMethod = "GET" | "POST";

export type JsonSchemaObject = {
  type?: string | string[];
  description?: string;
  enum?: unknown[];
  default?: unknown;
  properties?: Record<string, JsonSchemaObject>;
  required?: string[];
  additionalProperties?: boolean;
  items?: JsonSchemaObject;
  minimum?: number;
  maximum?: number;
};

export type McpToolContract = {
  name: string;
  title: string;
  description: string;
  category: string;
  implementation_status: string;
  endpoint_path: string;
  http_method: McpContractHttpMethod;
  requires_brain_id: boolean;
  scope_policy: string;
  permission_family: McpPermissionFamily;
  client_usage?: {
    when_to_use?: string;
    must_not?: string[];
    result_handling?: string[];
    followups?: string[];
    default_output_package?: string;
    mutation_policy?: string;
  };
  default_output_package: string;
  input_schema: JsonSchemaObject;
  output_schema: JsonSchemaObject;
  safety_contract?: {
    mutation_policy?: string;
    permission_family?: string;
    scope_policy?: string;
    requires_explicit_apply?: boolean;
    answer_demo_policy?: string;
  };
  backend_binding?: {
    binding_state?: string;
    candidate_backend_routes?: string[];
    implemented_in_slice?: string | null;
  };
};

export type McpContractRegistry = {
  schema_version: string;
  registry_status: string;
  tool_schema_version: string;
  guide_tool_names: string[];
  required_tool_names: string[];
  agent_memory_tool_names: string[];
  staged_tool_names: string[];
  tools: McpToolContract[];
  registry_validation?: {
    passed?: boolean;
    registered_tool_count?: number;
    required_tool_count?: number;
    schema_errors?: unknown[];
  };
};

export type McpRawExecutionPlan = {
  endpointPath: string;
  method: McpContractHttpMethod;
  payload: Record<string, unknown>;
  toolName: string;
};

export async function fetchMcpContractRegistry() {
  return fetchJson<McpContractRegistry>("/mcp/contracts", {
    method: "GET",
    timeoutMs: 20000,
  });
}

export async function executeMcpRawTool(plan: McpRawExecutionPlan) {
  if (plan.method === "GET") {
    return fetchJson<Record<string, unknown>>(endpointWithQuery(plan.endpointPath, plan.payload), {
      method: "GET",
      timeoutMs: timeoutForTool(plan.toolName),
    });
  }
  return fetchJson<Record<string, unknown>>(plan.endpointPath, {
    body: JSON.stringify(plan.payload),
    method: "POST",
    timeoutMs: timeoutForTool(plan.toolName),
  });
}

export function groupMcpToolsByPermissionFamily(tools: McpToolContract[]) {
  const groups = new Map<McpPermissionFamily, McpToolContract[]>();
  for (const tool of tools) {
    const family = normalizePermissionFamily(tool.permission_family);
    groups.set(family, [...(groups.get(family) || []), tool]);
  }
  return Array.from(groups.entries())
    .map(([family, familyTools]) => ({
      family,
      tools: [...familyTools].sort((left, right) => left.name.localeCompare(right.name)),
    }))
    .sort((left, right) => permissionFamilyOrder(left.family) - permissionFamilyOrder(right.family));
}

export function samplePayloadForMcpTool(tool: McpToolContract, activeBrainId: string) {
  const payload = schemaSampleObject(tool.input_schema);
  if (tool.requires_brain_id && activeBrainId && tool.http_method !== "GET") {
    payload.brain_id = activeBrainId;
  }
  return payload;
}

export function permissionFamilyRequiresConfirmation(family: McpPermissionFamily) {
  return family === "explicit_apply" || family === "destructive";
}

export function mcpRawConfirmationPhrase(tool: McpToolContract) {
  return `RUN ${tool.name}`;
}

function schemaSampleObject(schema: JsonSchemaObject): Record<string, unknown> {
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  const result: Record<string, unknown> = {};
  for (const [key, property] of Object.entries(properties)) {
    if (property.default !== undefined) {
      result[key] = property.default;
    } else if (required.has(key)) {
      result[key] = sampleValueForSchema(key, property);
    }
  }
  return result;
}

function sampleValueForSchema(key: string, schema: JsonSchemaObject): unknown {
  if (schema.enum?.length) return schema.enum[0];
  const type = Array.isArray(schema.type) ? schema.type.find((item) => item !== "null") : schema.type;
  if (type === "boolean") return false;
  if (type === "integer" || type === "number") return schema.minimum ?? 1;
  if (type === "array") return [];
  if (type === "object") return {};
  if (key === "query_text") return "What should AGVM retrieve from this brain?";
  if (key === "search_id") return "paste_search_id_here";
  if (key === "node_id") return "paste_node_id_here";
  if (key === "display_name") return "AGVM Project Memory";
  return "";
}

function endpointWithQuery(endpointPath: string, payload: Record<string, unknown>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || value === undefined || value === "") continue;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      params.set(key, String(value));
    }
  }
  const query = params.toString();
  return query ? `${endpointPath}?${query}` : endpointPath;
}

function normalizePermissionFamily(family: string): McpPermissionFamily {
  if (
    family === "read_only" ||
    family === "read_only_export" ||
    family === "registry_write" ||
    family === "preview_only" ||
    family === "explicit_apply" ||
    family === "destructive"
  ) {
    return family;
  }
  return "read_only";
}

function permissionFamilyOrder(family: McpPermissionFamily) {
  return {
    read_only: 0,
    read_only_export: 1,
    registry_write: 2,
    preview_only: 3,
    explicit_apply: 4,
    destructive: 5,
  }[family];
}

function timeoutForTool(toolName: string) {
  if (toolName.startsWith("retrieve_") || toolName.startsWith("inspect_")) return 180000;
  if (toolName.includes("apply") || toolName.includes("commit")) return 180000;
  return 60000;
}
