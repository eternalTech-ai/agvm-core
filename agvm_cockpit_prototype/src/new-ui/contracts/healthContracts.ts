import { asArray, asBoolean, asNumber, asObject, asString, compactLabel, stringList, type JsonObject } from "./contractUtils";

export type BrainHealthAlertView = {
  alertId: string;
  endpointHint: string;
  impact: string;
  reasonCodes: string[];
  severity: string;
  signalFamily: string;
};

export type BrainHealthCheckView = {
  key: string;
  label: string;
  score: number | null;
  status: string;
  summary: string;
};

export type BenchmarkPreflightView = {
  revolutionaryCertificationAllowed: boolean;
  seriousBenchmarkAllowed: boolean;
  verdict: string;
};

export type EvolutionRecommendationView = {
  endpointHint: string;
  primaryRecommendation: string;
};

export type HealthAuditViewModel = {
  alertCount: number;
  alerts: BrainHealthAlertView[];
  benchmarkPreflight: BenchmarkPreflightView;
  checks: BrainHealthCheckView[];
  generatedAt: string;
  original: JsonObject;
  readiness: string;
  recommendation: string;
  reasonCodes: string[];
  score: number | null;
  summary: string;
  evolutionRecommendation: EvolutionRecommendationView;
};

export type BrainHealthToolRequest = {
  brainId?: string | null;
  includeIssueSamples?: boolean;
  limit?: number;
};

export type BrainHealthToolResponse = JsonObject;

const CHECK_LABELS: Record<string, string> = {
  document_retrievability: "Document retrievability",
  identity_explicitness: "Identity explicitness",
  link_coherence: "Link coherence",
  metamemory: "Metamemory",
  node_atomicity: "Node atomicity",
  radial_distribution: "Radial distribution",
  recent_retrieval_failures: "Recent retrieval failures",
  retrieval_learning_rollup: "Retrieval learning",
  source_coverage: "Source coverage",
};

export function normalizeBrainHealthResponse(response: unknown): HealthAuditViewModel {
  const root = asObject(response);
  const report = asObject(root.brain_health_report);
  const checksRoot = asObject(root.checks ?? report.checks);
  const benchmark = asObject(root.benchmark_preflight ?? report.benchmark_preflight);
  const evolution = asObject(root.evolution_recommendation ?? report.evolution_recommendation);
  const sanity = asObject(root.brain_sanity_snapshot ?? report.brain_sanity_snapshot);
  const alerts = asArray(root.health_alerts ?? report.health_alerts).map(normalizeAlert);
  const reasonCodes = stringList(root.reason_codes ?? report.reason_codes, 64);
  const score = scoreFrom(report.overall_score ?? root.overall_score ?? report.score);

  return {
    alertCount: alerts.length,
    alerts,
    benchmarkPreflight: {
      revolutionaryCertificationAllowed: asBoolean(benchmark.revolutionary_certification_allowed),
      seriousBenchmarkAllowed: asBoolean(benchmark.serious_product_benchmark_allowed),
      verdict: asString(benchmark.verdict, "unknown"),
    },
    checks: normalizeChecks(checksRoot),
    evolutionRecommendation: {
      endpointHint: asString(evolution.endpoint_hint),
      primaryRecommendation: asString(evolution.primary_recommendation, asString(root.recommendation ?? report.recommendation, "none")),
    },
    generatedAt: asString(root.generated_at ?? report.generated_at ?? sanity.generated_at),
    original: root,
    readiness: asString(report.readiness ?? root.readiness ?? report.status, "unknown"),
    recommendation: asString(root.recommendation ?? report.recommendation, "none"),
    reasonCodes,
    score,
    summary: healthSummary(report.summary ?? root.health_summary ?? root.summary),
  };
}

function normalizeAlert(value: unknown): BrainHealthAlertView {
  const alert = asObject(value);
  return {
    alertId: asString(alert.alert_id, "alert"),
    endpointHint: asString(alert.endpoint_hint),
    impact: asString(alert.product_gate_impact, "informational"),
    reasonCodes: stringList(alert.reason_codes, 8),
    severity: asString(alert.severity, "watch"),
    signalFamily: asString(alert.signal_family, "unknown"),
  };
}

function normalizeChecks(checksRoot: JsonObject): BrainHealthCheckView[] {
  const entries = Object.entries(CHECK_LABELS);
  return entries.map(([key, label]) => {
    const check = asObject(checksRoot[key]);
    const status = asString(check.status ?? check.verdict ?? check.state, "unknown");
    const score = scoreFrom(check.score ?? check.ratio ?? check.value);
    const reasonCodes = stringList(check.reason_codes ?? check.reasons, 4);
    const summary = asString(check.summary ?? check.message, reasonCodes.length ? reasonCodes.join(", ") : checkMetricSummary(check));
    return { key, label, score, status, summary: compactLabel(summary, "No detail returned.", 120) };
  });
}

function scoreFrom(value: unknown): number | null {
  if (value === undefined || value === null || value === "") return null;
  return Math.max(0, Math.min(1, asNumber(value)));
}

function healthSummary(value: unknown): string {
  const text = asString(value);
  if (text) return text;
  const summary = asObject(value);
  const nodeCount = asNumber(summary.node_count);
  const anchors = asNumber(summary.document_anchor_count);
  const nonMutating = summary.health_is_non_mutating;
  const parts = [
    nodeCount ? `${nodeCount.toLocaleString()} nodes` : "",
    anchors ? `${anchors.toLocaleString()} document anchors` : "",
    nonMutating !== undefined ? `non-mutating=${String(nonMutating)}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" / ") : "No backend summary returned.";
}

function checkMetricSummary(check: JsonObject): string {
  const ignored = new Set(["score", "schema_version", "sample", "samples"]);
  const metrics = Object.entries(check)
    .filter(([key, value]) => !ignored.has(key) && (typeof value === "number" || typeof value === "string" || typeof value === "boolean"))
    .slice(0, 3)
    .map(([key, value]) => `${key.replace(/_/g, " ")}=${String(value)}`);
  return metrics.length ? metrics.join(" / ") : "No detail returned.";
}
