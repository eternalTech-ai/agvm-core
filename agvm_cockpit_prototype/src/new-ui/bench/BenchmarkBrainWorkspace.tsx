import { Activity, AlertTriangle, BarChart3, CheckCircle2, Database, ExternalLink, FileText, GitBranch, ShieldCheck, Timer } from "lucide-react";

import type { OpsWorkspaceContext } from "../ops/opsWorkspaceTypes";
import { ProductPageFrame } from "../shell/ProductPageFrame";
import {
  BenchmarkPreflight,
  benchmarkBaselineRows,
  benchmarkClaimLedger,
  benchmarkFamilyRows,
  benchmarkGates,
  bestComparableBaselineQuality,
  externalSciFactFullArtifact,
  formatLatency,
  formatPercent,
  formatScore,
  localRetrieveCertificationArtifact,
} from "./benchmarkCertification";

export function BenchmarkBrainWorkspace({ context, embeddedInProductShell = false }: { context: OpsWorkspaceContext; embeddedInProductShell?: boolean }) {
  const bestBaseline = bestComparableBaselineQuality();
  const qualityLift = localRetrieveCertificationArtifact.averageQuality - bestBaseline;
  return (
    <ProductPageFrame
      actions={[]}
      chrome={embeddedInProductShell ? "embedded" : "full"}
      className="bench-certification-frame"
      eyebrow="Certification"
      icon={ShieldCheck}
      intent="Artifact-backed benchmark evidence, scoped claims and remaining product gates."
      metrics={[
        { label: "Local MCP", value: `${localRetrieveCertificationArtifact.passedCaseCount} / ${localRetrieveCertificationArtifact.caseCount}`, detail: "internal matrix" },
        { label: "SciFact", value: `${externalSciFactFullArtifact.passedQueryCount} / ${externalSciFactFullArtifact.queryCount}`, detail: externalSciFactFullArtifact.certifiedLevel },
        { label: "Loaded graph", value: context.graphNodeCount.toLocaleString(), detail: `${context.totalNodeCount.toLocaleString()} total nodes` },
      ]}
      mode="benchmarks"
      status="Artifact-backed / UI gated"
      title="Benchmark Control Room"
    >
      <section className="bench-certification-workspace">
        <section className="bench-command-strip" aria-label="Benchmark status summary">
          <div className="bench-proof-summary">
            <span>Certified scope</span>
            <strong>Local MCP + SciFact Level 2 are green. UI is scoped, not hosted/cloud.</strong>
          </div>
          <div className="bench-proof-kpis">
            <MetricTile label="Green gates" value={`${BenchmarkPreflight.certifiedGateCount}`} detail={`${BenchmarkPreflight.blockedGateCount} blocked`} />
            <MetricTile label="Local matrix" value={`${localRetrieveCertificationArtifact.passedCaseCount}/${localRetrieveCertificationArtifact.caseCount}`} detail="internal MCP" />
            <MetricTile label="SciFact" value={`${externalSciFactFullArtifact.passedQueryCount}/${externalSciFactFullArtifact.queryCount}`} detail="document evidence" />
            <MetricTile label="Graph loaded" value={context.graphNodeCount.toLocaleString()} detail={`${context.totalNodeCount.toLocaleString()} total`} />
          </div>
        </section>

        <section className="bench-gate-grid" aria-label="Benchmark gates">
          {benchmarkGates.map((gate) => (
            <article className={`bench-gate-card bench-gate-${gate.tone}`} key={gate.id}>
              <div className="bench-gate-head">
                {gate.tone === "certified" ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}
                <div>
                  <span>{gate.label}</span>
                  <strong>{gate.value}</strong>
                </div>
                <em>{gate.status}</em>
              </div>
              <p>{gate.detail}</p>
              <ul className="bench-proof-chip-list">
                {gate.proof.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
              <small className="bench-artifact-path" title={gate.evidencePath}>{gate.evidencePath}</small>
            </article>
          ))}
        </section>

        <section className="bench-product-row bench-product-row-priority">
          <article className="bench-panel bench-score-panel">
            <PanelHead icon={BarChart3} label="Internal comparison" value={`+${formatScore(qualityLift)} quality lift`} />
            <div className="bench-score-list">
              {benchmarkBaselineRows.map((row) => (
                <div className={`bench-score-row ${row.id === "agvm_full_ai_core" ? "primary" : ""}`} key={row.id}>
                  <div className="bench-score-copy">
                    <strong>{row.label}</strong>
                    <span>{formatScore(row.quality)} quality / {formatLatency(row.latencyMs)}</span>
                  </div>
                  <div className="bench-score-meter" aria-label={`${row.label} quality ${formatScore(row.quality)}`}>
                    <i style={{ width: `${Math.min(100, row.quality * 100)}%` }} />
                  </div>
                  <em>{row.id === "agvm_full_ai_core" ? "AGVM" : "baseline"}</em>
                </div>
              ))}
            </div>
          </article>

          <div className="bench-side-stack">
            <article className="bench-panel bench-scifact-panel">
              <PanelHead icon={ExternalLink} label="External SciFact Level 2" value={`${externalSciFactFullArtifact.passedQueryCount} / ${externalSciFactFullArtifact.queryCount}`} />
              <div className="bench-scifact-table">
                <MetricTile label="nDCG@10" value={`${formatScore(externalSciFactFullArtifact.agvmNdcg10)} vs ${formatScore(externalSciFactFullArtifact.bm25Ndcg10)}`} detail="AGVM vs BM25" />
                <MetricTile label="Recall@10" value={`${formatScore(externalSciFactFullArtifact.agvmRecall10)} vs ${formatScore(externalSciFactFullArtifact.bm25Recall10)}`} detail="AGVM vs BM25" />
                <MetricTile label="MRR@10" value={`${formatScore(externalSciFactFullArtifact.agvmMrr10)} vs ${formatScore(externalSciFactFullArtifact.bm25Mrr10)}`} detail="AGVM vs BM25" />
                <MetricTile label="Terminality" value={formatPercent(externalSciFactFullArtifact.terminality)} detail="live MCP" />
              </div>
            </article>

            <article className="bench-panel bench-latency-panel">
              <PanelHead icon={Timer} label="Latency evidence" value={`p95 ${formatLatency(localRetrieveCertificationArtifact.firstPayloadP95Ms)}`} />
              <div className="bench-latency-strip">
                <MetricTile label="Local p50" value={formatLatency(localRetrieveCertificationArtifact.firstPayloadP50Ms)} detail="first payload" />
                <MetricTile label="Local max" value={formatLatency(localRetrieveCertificationArtifact.firstPayloadMaxMs)} detail="path outlier" />
                <MetricTile label="SciFact avg" value={formatLatency(externalSciFactFullArtifact.averageLatencyMs)} detail="document evidence" />
              </div>
            </article>
          </div>
        </section>

        <section className="bench-product-row bench-product-row-native">
          <article className="bench-panel bench-native-panel">
            <PanelHead icon={Database} label="AGVM-native metrics" value="not exposed by RAG" />
            <div className="bench-native-metrics">
              <MetricTile label="Hot/cold continuity" value={formatScore(localRetrieveCertificationArtifact.hotColdContinuity)} detail="baseline 0" />
              <MetricTile label="Maintenance awareness" value={formatScore(localRetrieveCertificationArtifact.maintenanceAwareness)} detail="baseline 0" />
              <MetricTile label="Expected coverage" value={formatScore(localRetrieveCertificationArtifact.expectedCoverage)} detail="best baseline 0.679" />
              <MetricTile label="Document actionability" value={formatScore(externalSciFactFullArtifact.documentActionability)} detail="SciFact live MCP" />
            </div>
          </article>

          <article className="bench-panel bench-family-panel">
            <PanelHead icon={GitBranch} label="Family coverage" value="12 / 12 families green" />
            <div className="bench-family-grid">
              {benchmarkFamilyRows.map((row) => (
                <div className={row.id === "path_corridor" ? "watch" : ""} key={row.id}>
                  <strong>{row.label}</strong>
                  <span>{row.count} cases / q {formatScore(row.averageQuality)}</span>
                  <em>{formatLatency(row.maxLatencyMs)} max</em>
                </div>
              ))}
            </div>
          </article>
        </section>

        <section className="bench-product-row bench-product-row-claims">
          <article className="bench-panel bench-claim-panel">
            <PanelHead icon={FileText} label="Claim ledger" value="truth-scoped" />
            <div className="bench-claim-list">
              {benchmarkClaimLedger.map((claim) => (
                <div className={claim.label.toLowerCase()} key={`${claim.label}:${claim.value}`}>
                  <span>{claim.label}</span>
                  <strong>{claim.value}</strong>
                  <p>{claim.detail}</p>
                </div>
              ))}
            </div>
          </article>
        </section>
      </section>
    </ProductPageFrame>
  );
}

function PanelHead({ icon: Icon, label, value }: { icon: typeof Activity; label: string; value: string }) {
  return (
    <header className="bench-panel-head">
      <Icon size={17} />
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
    </header>
  );
}

function MetricTile({ detail, label, value }: { detail: string; label: string; value: string }) {
  return (
    <div className="bench-metric-tile">
      <span>{label}</span>
      <strong>{value}</strong>
      <em>{detail}</em>
    </div>
  );
}
