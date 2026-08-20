import type { BrainRenderNode } from "../brain/brainSceneModel";
import type { HealthAuditViewModel } from "../contracts/healthContracts";
import type { MissionRouteSegment } from "../mission/missionProjection";

export function compactId(id: string) {
  return id.length > 18 ? `${id.slice(0, 8)}...${id.slice(-6)}` : id;
}

export function compactText(value: string, maxLength = 96) {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}...` : text;
}

export function compactNodeEvidence(node: BrainRenderNode) {
  const role = node.visualRole === "document" ? "document" : node.memory_type || node.node_kind || "memory";
  const text = node.summary || node.raw_text || node.source_unit_id || "No summary available";
  return `${role} - ${text.replace(/\s+/g, " ").slice(0, 112)}`;
}

export function routeSourceCounts(segments: MissionRouteSegment[]) {
  return segments.reduce(
    (counts, segment) => ({
      ...counts,
      [segment.source]: counts[segment.source] + 1,
    }),
    { highway: 0, link: 0, spatial: 0, run_projection: 0 },
  );
}

export function productGateLabel(audit: HealthAuditViewModel) {
  if (audit.benchmarkPreflight.revolutionaryCertificationAllowed) return "Certification allowed";
  if (audit.benchmarkPreflight.seriousBenchmarkAllowed) return "Warnings";
  if (audit.alertCount) return "Blocked";
  return audit.readiness || "Unknown";
}

export function healthStateLabel(audit: HealthAuditViewModel) {
  const recommendation = audit.recommendation === "none" ? "no action" : audit.recommendation;
  return `${audit.readiness || "unknown"} / ${recommendation}`;
}

export function boundedInteger(value: number, min: number, max: number) {
  const numeric = Number.isFinite(value) ? Math.trunc(value) : min;
  return Math.min(max, Math.max(min, numeric));
}

export function formatLatencyMs(value: number | null) {
  if (value === null) return "not run";
  if (value < 1000) return `${Math.max(1, Math.round(value))} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}

export function formatStringList(values: string[], limit = 5) {
  const head = values.slice(0, limit);
  const suffix = values.length > limit ? ` +${values.length - limit}` : "";
  return head.join(", ") + suffix || "none";
}

export function formatNullableBoolean(value: boolean | null) {
  if (value === true) return "true";
  if (value === false) return "false";
  return "not reported";
}

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Unknown AGVM API error";
}
