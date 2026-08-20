export type BenchmarkGateTone = "certified" | "warning" | "blocked";

export type BenchmarkGate = {
  detail: string;
  evidencePath: string;
  id: string;
  label: string;
  proof: string[];
  status: string;
  tone: BenchmarkGateTone;
  value: string;
};

export type BenchmarkFamilyRow = {
  averageQuality: number;
  count: number;
  id: string;
  label: string;
  maxLatencyMs: number;
  qualityDelta: number;
};

export type BenchmarkBaselineRow = {
  expectedCoverage: number;
  hotColdContinuity: number;
  id: string;
  label: string;
  latencyMs: number;
  maintenanceAwareness: number;
  quality: number;
};

export const localRetrieveCertificationArtifact = {
  artifactPath: "tmp_benchmark/phase8c_c_full_certification_20260601_165744.json",
  brainId: "simone_massaro_validation",
  caseCount: 130,
  passedCaseCount: 130,
  allPass: true,
  averageQuality: 0.847888,
  minimumQualityDelta: 0.087963,
  averageLatencyMs: 3492.031,
  firstPayloadP50Ms: 2484,
  firstPayloadP95Ms: 8104,
  firstPayloadMaxMs: 57207,
  expectedCoverage: 1,
  hotColdContinuity: 0.553846,
  maintenanceAwareness: 1,
  documentActionability: 0.690385,
  pathTruth: 0.221538,
};

export const localMcpProductVerdictArtifact = {
  artifactPath: "tmp_benchmark/phase8d_final_after_warning_gate_20260530_194504/real_final_product_verdict_after_warning_gate_20260530_194504.json",
  allPass: true,
  localMcpProductReady: true,
  verdict: "local_mcp_product_ready",
  scope: "self-hosted local MCP backend; not hosted/cloud and not final UI certification",
};

export const externalSciFactFullArtifact = {
  artifactPath: "tmp_external_benchmark/scifact_anchor_cert/dwe8_live_full_300_20260602/external_certification_20260602_150742.json",
  brainId: "external_scifact_anchor_20260531",
  dataset: "SciFact live MCP document evidence",
  queryCount: 300,
  passedQueryCount: 300,
  allPass: true,
  agvmNdcg10: 0.673663,
  bm25Ndcg10: 0.66439,
  agvmRecall10: 0.786833,
  bm25Recall10: 0.781611,
  agvmMrr10: 0.643811,
  bm25Mrr10: 0.633721,
  terminality: 1,
  rawAvailability: 1,
  documentActionability: 1,
  pathTruth: 0.736667,
  averageLatencyMs: 23084.17,
  p50LatencyMs: 22167,
  maxLatencyMs: 42046.47,
  certifiedLevel: "Level 2 live MCP document evidence",
  unclaimedLevel: "Level 3 context faithfulness remains unclaimed",
};

export const benchmarkBaselineRows: BenchmarkBaselineRow[] = [
  {
    id: "agvm_full_ai_core",
    label: "AGVM full AI core",
    quality: localRetrieveCertificationArtifact.averageQuality,
    latencyMs: localRetrieveCertificationArtifact.averageLatencyMs,
    expectedCoverage: 1,
    hotColdContinuity: localRetrieveCertificationArtifact.hotColdContinuity,
    maintenanceAwareness: 1,
  },
  {
    id: "vector_hash_rag",
    label: "Vector/hash RAG",
    quality: 0.497342,
    latencyMs: 36.281,
    expectedCoverage: 0.679487,
    hotColdContinuity: 0,
    maintenanceAwareness: 0,
  },
  {
    id: "graph_neighbor_no_ai",
    label: "Graph no-AI",
    quality: 0.496254,
    latencyMs: 37.934,
    expectedCoverage: 0.628205,
    hotColdContinuity: 0,
    maintenanceAwareness: 0,
  },
  {
    id: "agvm_heuristic_only",
    label: "AGVM heuristic-only",
    quality: 0.494814,
    latencyMs: 48.778,
    expectedCoverage: 0.671795,
    hotColdContinuity: 0,
    maintenanceAwareness: 0,
  },
  {
    id: "bm25_lexical",
    label: "BM25 lexical",
    quality: 0.485976,
    latencyMs: 36.07,
    expectedCoverage: 0.653846,
    hotColdContinuity: 0,
    maintenanceAwareness: 0,
  },
  {
    id: "hybrid_lexical_vector",
    label: "Hybrid lexical/vector",
    quality: 0.47963,
    latencyMs: 36.958,
    expectedCoverage: 0.638462,
    hotColdContinuity: 0,
    maintenanceAwareness: 0,
  },
];

export const benchmarkFamilyRows: BenchmarkFamilyRow[] = [
  { id: "broad_dossier", label: "Broad dossier", count: 10, averageQuality: 0.925239, qualityDelta: 0.231727, maxLatencyMs: 6061 },
  { id: "company_work_relation", label: "Company/work relation", count: 15, averageQuality: 0.849801, qualityDelta: 0.193846, maxLatencyMs: 12777 },
  { id: "document_raw", label: "Document raw", count: 10, averageQuality: 0.808704, qualityDelta: 0.099815, maxLatencyMs: 4334 },
  { id: "followup_hot_context", label: "Follow-up hot context", count: 10, averageQuality: 0.881611, qualityDelta: 0.201713, maxLatencyMs: 15190 },
  { id: "identity_exact", label: "Identity exact", count: 15, averageQuality: 0.850037, qualityDelta: 0.232271, maxLatencyMs: 11502 },
  { id: "multi_intent", label: "Multi-intent", count: 10, averageQuality: 0.915702, qualityDelta: 0.228116, maxLatencyMs: 7726 },
  { id: "no_match_boundary", label: "No-match boundary", count: 10, averageQuality: 0.735032, qualityDelta: 0.609242, maxLatencyMs: 294 },
  { id: "operations_health", label: "Operations health", count: 10, averageQuality: 0.823245, qualityDelta: 0.177259, maxLatencyMs: 5497 },
  { id: "path_corridor", label: "Path corridor", count: 10, averageQuality: 0.799113, qualityDelta: 0.263995, maxLatencyMs: 57207 },
  { id: "relationship_boundary", label: "Relationship boundary", count: 10, averageQuality: 0.8622, qualityDelta: 0.499567, maxLatencyMs: 7067 },
  { id: "timeline_event", label: "Timeline event", count: 10, averageQuality: 0.852041, qualityDelta: 0.281188, maxLatencyMs: 5325 },
  { id: "values_style", label: "Values/style", count: 10, averageQuality: 0.8699, qualityDelta: 0.6633, maxLatencyMs: 11725 },
];

export const benchmarkGates: BenchmarkGate[] = [
  {
    id: "local_mcp_matrix",
    label: "Local MCP matrix",
    value: `${localRetrieveCertificationArtifact.passedCaseCount} / ${localRetrieveCertificationArtifact.caseCount}`,
    status: "green",
    detail: "Internal MCP retrieve/RAG comparison passed on the Simone validation brain.",
    tone: "certified",
    evidencePath: localRetrieveCertificationArtifact.artifactPath,
    proof: [
      `Average AGVM quality ${formatScore(localRetrieveCertificationArtifact.averageQuality)}`,
      `Minimum quality delta ${formatScore(localRetrieveCertificationArtifact.minimumQualityDelta)}`,
      `Expected coverage ${formatPercent(localRetrieveCertificationArtifact.expectedCoverage)}`,
    ],
  },
  {
    id: "external_scifact_live_mcp",
    label: "External SciFact evidence",
    value: `${externalSciFactFullArtifact.passedQueryCount} / ${externalSciFactFullArtifact.queryCount}`,
    status: "certified",
    detail: "Level 2 live MCP document-evidence proof beats the BM25 baseline on nDCG, Recall and MRR.",
    tone: "certified",
    evidencePath: externalSciFactFullArtifact.artifactPath,
    proof: [
      `nDCG@10 ${formatScore(externalSciFactFullArtifact.agvmNdcg10)} vs BM25 ${formatScore(externalSciFactFullArtifact.bm25Ndcg10)}`,
      `terminality ${formatPercent(externalSciFactFullArtifact.terminality)}`,
      externalSciFactFullArtifact.unclaimedLevel,
    ],
  },
  {
    id: "local_mcp_product_verdict",
    label: "Local backend product verdict",
    value: localMcpProductVerdictArtifact.localMcpProductReady ? "ready" : "blocked",
    status: localMcpProductVerdictArtifact.verdict,
    detail: localMcpProductVerdictArtifact.scope,
    tone: "certified",
    evidencePath: localMcpProductVerdictArtifact.artifactPath,
    proof: ["Backend MCP contract green", "Self-hosted local scope only", "UI/browser readiness evaluated separately"],
  },
  {
    id: "ui_product_ready",
    label: "Local cockpit UI",
    value: "scoped_ready",
    status: "cert-ui-2 complete",
    detail: "Local self-hosted read-only/preview/operator cockpit passed final browser certification across pages, reload and document hydration.",
    tone: "certified",
    evidencePath: "tmp_ui_validation/certui2_product_ui_20260605_matrix/",
    proof: ["UI-BENCH-2 complete", "UI-SETTINGS-2 complete", "CERT-UI-2 complete", "24/24 browser matrix", "live Context/Results reload green", "document hydration green"],
  },
];

export const benchmarkClaimLedger = [
  {
    label: "Allowed",
    value: "local_mcp_backend_ready",
    detail: "Self-hosted backend MCP contract, not broad hosted product readiness.",
  },
  {
    label: "Allowed",
    value: "external_live_mcp_document_evidence_certified",
    detail: "SciFact Level 2 document-evidence proof with explicit Level 3 caveat.",
  },
  {
    label: "Scoped",
    value: "revolutionary_candidate",
    detail: "Only with the exact internal/external benchmark scope named.",
  },
  {
    label: "Scoped",
    value: "ui_product_ready_local_cockpit",
    detail: "Local read-only/preview/operator cockpit only; hosted/cloud, mutation apply and deeper autonomous chat remain separate gates.",
  },
];

export const BenchmarkPreflight = {
  certifiedGateCount: benchmarkGates.filter((gate) => gate.tone === "certified").length,
  blockedGateCount: benchmarkGates.filter((gate) => gate.tone === "blocked").length,
  healthGate: "latest Health UI reports product/retrieve allowed with certification caution",
  nextGate: "OPS-5B-B1 or ADL-H2+",
};

export function bestComparableBaselineQuality() {
  return Math.max(...benchmarkBaselineRows.filter((row) => row.id !== "agvm_full_ai_core").map((row) => row.quality));
}

export function formatScore(value: number) {
  return value.toFixed(3);
}

export function formatPercent(value: number) {
  return `${Math.round(value * 100)}%`;
}

export function formatLatency(ms: number) {
  if (ms >= 1000) return `${(ms / 1000).toFixed(ms >= 10000 ? 1 : 2)}s`;
  return `${Math.round(ms)}ms`;
}
