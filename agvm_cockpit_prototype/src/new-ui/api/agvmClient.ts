import { fetchJson } from "../../api/client";

export type AgvmHealth = {
  ok?: boolean;
  active_brain_id?: string;
  brain_registry_ready?: boolean;
  hosted_tenant_registry_ready?: boolean;
  hosted_tenant_registry?: Record<string, unknown>;
  service?: string;
  version?: string;
  runtime_scope_status?: string;
};

export type AgvmBrainSummary = {
  brain_id?: string;
  id?: string;
  display_name?: string;
  name?: string;
  description?: string | null;
  is_active?: boolean;
  is_default?: boolean;
  migration_status?: string;
  registry_brain_path?: string;
  storage_path?: string;
  storage_size_bytes?: number;
  source_manifest_path?: string;
  created_at?: string;
  node_count?: number;
  safe_for_mcp?: boolean;
  updated_at?: string;
  [key: string]: unknown;
};

export type AgvmBrainRegistryResponse = {
  schema_version?: string;
  registry_id?: string;
  active_brain_id?: string | null;
  default_brain_id?: string | null;
  brain_count?: number;
  brains?: AgvmBrainSummary[];
  brain_root?: string;
  registry_path?: string;
  legacy_data_dir_policy?: Record<string, unknown>;
  product_boundary?: Record<string, unknown>;
  storage_format_version?: string;
  validation?: Record<string, unknown>;
  runtime_scope_status?: string;
  next_slice?: string;
};

export type AgvmBrainAdminOperationResponse = {
  schema_version?: string;
  action?: string;
  status?: string;
  brain_id?: string | null;
  brain?: AgvmBrainSummary;
  registry?: AgvmBrainRegistryResponse;
  archive_path?: string | null;
  archive_size_bytes?: number | null;
  file_count?: number | null;
  export_manifest?: Record<string, unknown>;
  import_manifest?: Record<string, unknown>;
  warnings?: string[];
  next_slice?: string;
};

export type AgvmBrainCreateInput = {
  brain_id?: string;
  display_name: string;
  description?: string;
  make_active: boolean;
  make_default?: boolean;
};

export type AgvmBrainImportInput = {
  archive_path: string;
  brain_id?: string;
  display_name?: string;
  make_active: boolean;
  make_default?: boolean;
  overwrite_existing?: boolean;
};

export type AgvmGraphNode = {
  id: string;
  node_kind?: string | null;
  memory_type?: string | null;
  summary?: string | null;
  raw_text?: string | null;
  routing_brainhex?: AgvmBrainHex | null;
  semantic_color?: AgvmDisplayColor | null;
  final_position?: AgvmPosition | null;
  base_position?: AgvmPosition | null;
  topology_brainhex?: AgvmBrainHex | null;
  topology_color?: AgvmDisplayColor | null;
  is_document_anchor?: boolean | null;
  document_role?: string | null;
  source_unit_id?: string | null;
  links?: AgvmGraphLink[];
  highways?: AgvmGraphLink[];
};

export type AgvmPosition = {
  x?: number;
  y?: number;
  z?: number;
};

export type AgvmBrainHex = {
  theta_bin?: number;
  phi_bin?: number;
  radius_bin?: number;
  code?: string;
};

export type AgvmDisplayColor = {
  h?: number;
  s?: number;
  l?: number;
  hex?: string;
};

export type AgvmGraphLink = {
  target_node_id?: string;
  strength?: number;
  reason?: string | null;
  kind?: string | null;
};

export type AgvmGraphResponse = {
  graph?: {
    nodes?: AgvmGraphNode[];
    meta?: AgvmGraphMeta;
  };
};

export type AgvmGraphMeta = {
  sampled?: boolean;
  total_node_count?: number;
  sampled_node_count?: number;
  total_edge_count?: number;
  sampled_edge_count?: number;
  load_failed?: boolean;
  load_error?: string;
  progressive_loading?: boolean;
  progressive_preview_node_count?: number;
  progressive_target_node_count?: number;
  progressive_target_failed?: boolean;
  progressive_target_error?: string;
};

export async function getHealth() {
  return fetchJson<AgvmHealth>("/health", { timeoutMs: 5000 });
}

export async function getBrainRegistry() {
  try {
    return await fetchJson<AgvmBrainRegistryResponse>("/mcp/brains", { timeoutMs: 8000 });
  } catch {
    return fetchJson<AgvmBrainRegistryResponse>("/memory/brains", { timeoutMs: 8000 });
  }
}

export async function selectBrain(brainId: string) {
  return fetchJson<AgvmBrainRegistryResponse>("/memory/brains/select", {
    body: JSON.stringify({ brain_id: brainId, make_default: false }),
    method: "POST",
    timeoutMs: 15000,
  });
}

export async function createBrain(payload: AgvmBrainCreateInput) {
  return fetchJson<AgvmBrainAdminOperationResponse>("/memory/brains/create", {
    body: JSON.stringify(payload),
    method: "POST",
    timeoutMs: 30000,
  });
}

export async function importBrainArchive(payload: AgvmBrainImportInput) {
  return fetchJson<AgvmBrainAdminOperationResponse>("/memory/brains/import", {
    body: JSON.stringify(payload),
    method: "POST",
    timeoutMs: 120000,
  });
}

export async function getGraph(maxNodes = 1800, timeoutMs = 9000) {
  return fetchJson<AgvmGraphResponse>(`/graph-view?max_nodes=${maxNodes}`, { timeoutMs });
}
