import { fetchJson } from "../../api/client";
import type { BrainHealthToolRequest, BrainHealthToolResponse } from "../contracts/healthContracts";

export type GeometryCalibrationRequest = {
  brainId?: string | null;
  maxNodesConsidered?: number;
};

export function runBrainHealthTool(request: BrainHealthToolRequest = {}) {
  return fetchJson<BrainHealthToolResponse>("/mcp/brain-health", {
    body: JSON.stringify({
      brain_id: request.brainId || undefined,
      include_issue_samples: request.includeIssueSamples ?? false,
      limit: request.limit ?? 8,
    }),
    method: "POST",
    timeoutMs: 20000,
  });
}

export function getBrainHealth(request: BrainHealthToolRequest = {}) {
  const params = new URLSearchParams();
  if (request.limit !== undefined) params.set("limit", String(request.limit));
  if (request.includeIssueSamples !== undefined) params.set("include_issue_samples", String(request.includeIssueSamples));
  if (request.brainId) params.set("brain_id", request.brainId);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return fetchJson<BrainHealthToolResponse>(`/memory/brain-health${suffix}`, { timeoutMs: 12000 });
}

export function getGeometryCalibration(request: GeometryCalibrationRequest = {}) {
  const params = new URLSearchParams();
  if (request.brainId) params.set("brain_id", request.brainId);
  if (request.maxNodesConsidered !== undefined) params.set("max_nodes_considered", String(request.maxNodesConsidered));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return fetchJson<Record<string, unknown>>(`/memory/geometry-calibration${suffix}`, { timeoutMs: 20000 });
}
