import { AlertTriangle, ArrowRight, CheckCircle2, Database, GitBranch, HeartPulse, Loader2, RefreshCw, ShieldCheck, type LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { runBrainHealthTool } from "../api/healthClient";
import { Dropdown, type DropdownOption } from "../components/Dropdown";
import { normalizeBrainHealthResponse, type BrainHealthAlertView, type HealthAuditViewModel } from "../contracts/healthContracts";
import { brainOpsOverlayFromHealthAudit, createEmptyBrainOpsOverlay, overlaySummaryRows, type BrainOpsOverlay } from "../contracts/opsOverlayContracts";
import {
  BrainFirstWorkspace,
  ContractPanel,
  DefinitionRows,
  GuidedDecisionPager,
  GuidedInspectorPanel,
  MiniBrainContext,
  OperationReviewFrame,
  ReviewBatchDeltaMap,
  ReviewDecisionLedger,
  ReviewItemImpactPanel,
} from "./BrainFirstWorkspace";
import { healthOperationDeltas } from "./delta/operationDeltaAdapters";
import { operationDeltaRenderableState, summarizeOperationDeltas, type OperationDelta } from "./delta/operationDeltaContracts";
import { operationDeltaBatchReview, operationDeltaImpactSteps } from "./delta/operationDeltaReview";
import { OpsBrainPreview } from "./OpsBrainPreview";
import { compactText, errorMessage, healthStateLabel, productGateLabel } from "./opsFormatters";
import { readOpsSessionValue, writeOpsSessionValue } from "./opsSessionStorage";
import type { OpsWorkspaceContext } from "./opsWorkspaceTypes";

type HealthSessionSnapshot = {
  audit: HealthAuditViewModel;
};

function healthDevFixture(activeBrainId: string | null): HealthSessionSnapshot | null {
  if (!import.meta.env.DEV || typeof window === "undefined") return null;
  if (new URLSearchParams(window.location.search).get("healthFixture") !== "warning") return null;
  const response = {
    generated_at: "2026-06-14T10:42:18.000Z",
    brain_health_report: {
      readiness: "preview_required",
      overall_score: 0.78,
      summary: {
        node_count: 2017,
        document_anchor_count: 300,
        health_is_non_mutating: true,
      },
      recommendation: "matrix_calibration_preview",
      checks: {
        document_retrievability: { status: "green", score: 0.91, summary: "Document anchors are retrievable for the audited local scope." },
        identity_explicitness: { status: "green", score: 0.88, summary: "Identity anchors are explicit enough for local product use." },
        link_coherence: { status: "watch", score: 0.72, summary: "Some learned links need calibration before certification claims." },
        metamemory: { status: "green", score: 0.81, summary: "Hot/cold continuity is present." },
        node_atomicity: { status: "green", score: 0.84, summary: "Most source units produce inspectable atomic nodes." },
        radial_distribution: { status: "watch", score: 0.66, summary: "Spatial distribution should be recalibrated after recent growth." },
        recent_retrieval_failures: { status: "watch", score: 0.7, summary: "Recent retrieval misses are not fatal, but require review." },
        retrieval_learning_rollup: { status: "green", score: 0.82, summary: "Learning signals are available for the next preview." },
        source_coverage: { status: "green", score: 0.86, summary: "Source coverage is enough for local iteration." },
      },
      benchmark_preflight: {
        verdict: "preview_required_before_certification",
        serious_product_benchmark_allowed: false,
        revolutionary_certification_allowed: false,
      },
      evolution_recommendation: {
        primary_recommendation: "matrix_calibration_preview",
        endpoint_hint: "/mcp/matrix-calibration-preview",
      },
      health_alerts: [
        {
          alert_id: "health_fixture_matrix_calibration",
          endpoint_hint: "/mcp/matrix-calibration-preview",
          product_gate_impact: "requires_preview_before_serious_benchmark",
          reason_codes: ["radial_distribution:calibration_needed", "retrieval_learning:recent_growth"],
          severity: "action_recommended",
          signal_family: "heuristic_learning",
        },
        {
          alert_id: "health_fixture_context_watch",
          endpoint_hint: "",
          product_gate_impact: "benchmark_allowed_with_warning",
          reason_codes: ["context_quality:recent_misses"],
          severity: "watch",
          signal_family: "context_quality",
        },
      ],
    },
    validation_brain_rebuild_gate: {
      current_brain_usable_for_retrieve_benchmarks: true,
      product_proof_allowed_by_this_gate: false,
      status: "preview_required",
    },
  };
  return { audit: normalizeBrainHealthResponse(response) };
}

export function HealthBrainWorkspace({ context, embeddedInProductShell = false }: { context: OpsWorkspaceContext; embeddedInProductShell?: boolean }) {
  const [audit, setAudit] = useState<HealthAuditViewModel | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditRunning, setAuditRunning] = useState(false);
  const [issueLimit, setIssueLimit] = useState(8);
  const commandSurfaceRef = useRef<HTMLDivElement | null>(null);
  const activeBrainId = context.health?.active_brain_id || null;
  const productGate = audit ? productGateLabel(audit) : "Not audited";
  const productGateDetail = audit ? healthProductGateDetail(audit) : "run product health audit";
  const persistedHealthOverlay = context.opsOverlay?.mode === "health" ? context.opsOverlay : null;
  const healthOverlay = audit ? brainOpsOverlayFromHealthAudit(audit) : persistedHealthOverlay || createEmptyBrainOpsOverlay("health", "not_audited");
  const vitalState = healthVitalState(audit, auditRunning, auditError, healthOverlay.truth.hasSpatialCoordinates);

  useEffect(() => {
    if (auditRunning) return;
    const fixture = healthDevFixture(activeBrainId);
    if (fixture) {
      setAudit(fixture.audit);
      setAuditError(null);
      context.onOpsOverlayChange(brainOpsOverlayFromHealthAudit(fixture.audit));
      return;
    }
    const snapshot = readOpsSessionValue<HealthSessionSnapshot>("health-audit", activeBrainId);
    setAudit(snapshot?.audit || null);
    setAuditError(null);
  }, [activeBrainId]);

  useEffect(() => {
    if (!audit) return;
    writeOpsSessionValue<HealthSessionSnapshot>("health-audit", activeBrainId, { audit });
  }, [activeBrainId, audit]);

  useEffect(() => {
    if (!audit && !auditError) return;
    const resetScroll = () => commandSurfaceRef.current?.scrollTo({ left: 0, top: 0 });
    const frame = window.requestAnimationFrame(resetScroll);
    const timer = window.setTimeout(resetScroll, 80);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
    };
  }, [audit, auditError]);

  const runAudit = async () => {
    setAuditRunning(true);
    setAuditError(null);
    try {
      const response = await runBrainHealthTool({ brainId: activeBrainId, includeIssueSamples: false, limit: issueLimit });
      const normalized = normalizeBrainHealthResponse(response);
      const overlay = brainOpsOverlayFromHealthAudit(normalized);
      setAudit(normalized);
      context.onOpsOverlayChange(overlay);
    } catch (error) {
      setAuditError(errorMessage(error));
    } finally {
      setAuditRunning(false);
    }
  };

  return (
    <BrainFirstWorkspace
      actions={[]}
      embedded={embeddedInProductShell}
      icon={HeartPulse}
      eyebrow="Readiness"
      title="Health Workspace"
      intent="Read the real brain-health contract without pretending service reachability means product certification."
      status={audit ? healthStatusSummary(audit, vitalState) : context.health?.ok ? "Basic health online" : "Health pending"}
      metrics={[
        { label: "Active brain", value: context.health?.active_brain_id || "Unknown", detail: "health request scope" },
        { label: "Runtime", value: context.health?.runtime_scope_status || context.health?.service || "Unknown", detail: "basic check" },
        { label: "Product gate", value: productGate, detail: productGateDetail },
      ]}
      mode="health"
    >
      <OperationReviewFrame
        mode="health"
        decisionRef={commandSurfaceRef}
        brain={<HealthBrainScene audit={audit} auditError={auditError} auditRunning={auditRunning} context={context} overlay={healthOverlay} vitalState={vitalState} />}
        steps={healthGuidedSteps(audit, auditRunning, auditError)}
        context={
          <GuidedInspectorPanel
            eyebrow="Effect on brain"
            title={auditRunning ? "Diagnosing readiness" : audit ? "Readiness loaded" : "Run audit"}
            summary="Health reads readiness, benchmark gates and recommendations. It never creates memory, moves coordinates or applies changes."
            tone={healthInspectorTone(audit, auditRunning, auditError, vitalState)}
            rows={healthInspectorRows(audit, vitalState)}
          />
        }
        decision={
          <HealthGuidedDecisionPager
            activeBrainId={activeBrainId}
            audit={audit}
            auditError={auditError}
            auditRunning={auditRunning}
            issueLimit={issueLimit}
            onOpenGrow={context.chat?.onOpenGrow}
            onOpenMaintain={context.chat?.onOpenMaintain}
            onIssueLimitChange={setIssueLimit}
            onRunAudit={runAudit}
            vitalState={vitalState}
          />
        }
        proofTabs={[
          {
            id: "checks",
            label: "Checks",
            tone: audit ? "ready" : "mock",
            content: (
              <>
                <ContractPanel title="Brain Health Checks" tone={audit ? "ready" : "mock"} icon={CheckCircle2}>
                  {audit ? <HealthCheckGrid audit={audit} /> : <p className="mode-empty-copy">Run the product health audit to load backend checks.</p>}
                </ContractPanel>
                <ContractPanel title="Benchmark Preflight" tone={audit?.benchmarkPreflight.revolutionaryCertificationAllowed ? "ready" : audit ? "blocked" : "mock"} icon={Database}>
                  <DefinitionRows
                    rows={[
                      ["verdict", audit?.benchmarkPreflight.verdict || "not audited"],
                      ["serious benchmark", audit ? String(audit.benchmarkPreflight.seriousBenchmarkAllowed) : "not audited"],
                      ["revolutionary certification", audit ? String(audit.benchmarkPreflight.revolutionaryCertificationAllowed) : "not audited"],
                      ["recommendation", audit?.evolutionRecommendation.primaryRecommendation || "not audited"],
                      ["endpoint hint", audit?.evolutionRecommendation.endpointHint || "none"],
                    ]}
                  />
                </ContractPanel>
              </>
            ),
          },
          {
            id: "alerts",
            label: "Alerts",
            tone: audit?.alerts.length ? "blocked" : audit ? "ready" : "mock",
            content: (
              <ContractPanel title="Health Alerts" tone={audit?.alerts.length ? "blocked" : audit ? "ready" : "mock"} icon={AlertTriangle}>
                {audit ? <HealthAlertList audit={audit} /> : <p className="mode-empty-copy">No alert contract loaded yet.</p>}
              </ContractPanel>
            ),
          },
          {
            id: "contract",
            label: "Contract",
            tone: audit || persistedHealthOverlay ? "ready" : "mock",
            content: (
              <>
                <ContractPanel title="Brain Overlay Contract" tone={audit || persistedHealthOverlay ? "ready" : "mock"} icon={GitBranch}>
                  <DefinitionRows rows={overlaySummaryRows(healthOverlay)} />
                  {!healthOverlay.truth.hasSpatialCoordinates ? (
                    <p className="mode-contract-copy">
                      No 3D Health overlay is drawn yet because this backend response did not expose target ids or coordinates. Renderer integration waits for real spatial evidence.
                    </p>
                  ) : null}
                </ContractPanel>
                <MiniBrainContext context={context} />
              </>
            ),
          },
        ]}
      />
    </BrainFirstWorkspace>
  );
}

function HealthGuidedDecisionPager({
  activeBrainId,
  audit,
  auditError,
  auditRunning,
  issueLimit,
  onOpenGrow,
  onOpenMaintain,
  onIssueLimitChange,
  onRunAudit,
  vitalState,
}: {
  activeBrainId: string | null;
  audit: HealthAuditViewModel | null;
  auditError: string | null;
  auditRunning: boolean;
  issueLimit: number;
  onOpenGrow?: () => void;
  onOpenMaintain?: (mode?: "sleep" | "evolve" | "matrix") => void;
  onIssueLimitChange: (value: number) => void;
  onRunAudit: () => void;
  vitalState: HealthVitalState;
}) {
  const endpoint = audit?.evolutionRecommendation.endpointHint || "none";
  const recommendation = audit?.evolutionRecommendation.primaryRecommendation || audit?.recommendation || "none";
  const firstAlert = audit ? primaryHealthAlert(audit) : null;
  const healthScore = vitalState.score === null ? "pending" : `${Math.round(vitalState.score * 100)}%`;
  const decision = healthDecisionCopy(audit, auditError, auditRunning, vitalState);
  const nextAction = healthNextActionCopy(audit);
  const healthStepOverride = healthGuidedStepOverride();
  const runAction = (
    <button className="health-audit-button" disabled={auditRunning} onClick={onRunAudit} type="button">
      {auditRunning ? <Loader2 size={15} /> : <RefreshCw size={15} />}
      <span>{auditRunning ? "Running health check" : audit ? "Refresh health check" : "Run health check"}</span>
    </button>
  );

  return (
    <GuidedDecisionPager
      ariaLabel="Health guided flow"
      autoFocusKey={healthPagerAutoFocusKey(audit, auditRunning, auditError)}
      autoFocusPageId={healthStepOverride || (auditRunning || auditError ? "run" : audit ? "verdict" : "run")}
      pages={[
        {
          content: (
            <HealthAuditRunBoard
              activeBrainId={activeBrainId}
              audit={audit}
              auditError={auditError}
              auditRunning={auditRunning}
              issueLimit={issueLimit}
              onIssueLimitChange={onIssueLimitChange}
            />
          ),
          id: "run",
          isComplete: Boolean(audit) && !auditRunning && !auditError,
          kicker: "Step 1",
          label: "Check",
          primaryAction: runAction,
          rows: [
            ["What it does", "read-only product health check"],
            ["Backend tool", "/mcp/brain-health"],
            ["Mutation", "never"],
          ],
          blockedReason: "Run the health check before inspecting verdict",
          summary: "Health does not repair the brain. It checks whether the current brain is trustworthy enough for product work and benchmarks.",
          statusLabel: auditError ? "Health check blocked" : auditRunning ? "Checking" : audit ? "Health check loaded" : "Health check required",
          title: auditRunning ? "Checking brain health" : audit ? "Health check loaded" : "Run the health check",
          tone: auditRunning ? "live" : audit ? "ready" : auditError ? "blocked" : "idle",
        },
        {
          canEnter: Boolean(audit),
          content: <HealthReadinessCommandBoard audit={audit} auditError={auditError} auditRunning={auditRunning} vitalState={vitalState} />,
          id: "verdict",
          isComplete: Boolean(audit),
          kicker: "Step 2",
          label: "Verdict",
          rows: [
            ["Product use", decision.productWorkTitle],
            ["Health score", healthScore],
            ["Certification", decision.certificationTitle],
          ],
          blockedReason: "Run the health check before verdict",
          statusLabel: audit ? "Verdict ready" : "Health check required",
          summary: decision.body,
          title: decision.shortTitle,
          tone: auditError ? "blocked" : auditRunning ? "live" : audit ? vitalState.tone === "blocked" ? "blocked" : vitalState.tone === "warning" ? "ready" : "ready" : "idle",
        },
        {
          canEnter: Boolean(audit),
          content: audit ? <HealthIssueReview audit={audit} vitalState={vitalState} /> : <p className="mode-empty-copy">Run the health check to see human-readable issues and backend checks.</p>,
          id: "issues",
          isComplete: Boolean(audit),
          kicker: "Step 3",
          label: "Issues",
          rows: [
            ["Alerts", audit ? String(audit.alertCount) : "not audited"],
            ["First issue", firstAlert ? healthAlertFamilyLabel(firstAlert.signalFamily) : audit ? "none" : "not audited"],
            ["Geometry", vitalState.hasSpatialTargets ? "targeted alert coordinates" : audit ? "diagnostic only, no coordinates" : "pending"],
          ],
          blockedReason: "Run the health check before issue review",
          statusLabel: audit ? "Issues reviewed" : "Health check required",
          summary: "Issues are diagnostic evidence. If Health has no coordinates for an issue, the UI must not fake an affected brain region.",
          title: firstAlert ? healthAlertImpactSentence(firstAlert) : audit ? "No blocking issue returned" : "No issue list yet",
          tone: firstAlert ? "blocked" : audit ? "ready" : auditRunning ? "live" : "idle",
        },
        {
          canEnter: Boolean(audit),
          content: <HealthNextActionPanel audit={audit} onOpenGrow={onOpenGrow} onOpenMaintain={onOpenMaintain} />,
          id: "next",
          isComplete: Boolean(audit),
          kicker: "Step 4",
          label: "Recommend",
          rows: [
            ["Recommended preview", nextAction.title],
            ["MCP endpoint", endpoint],
            ["Apply", "locked"],
          ],
          blockedReason: "Run the health check before recommendations",
          statusLabel: audit ? "Recommendation ready" : "Health check required",
          summary: "Health can only recommend the next preview. It never applies Sleep, Evolve, Matrix or Grow changes by itself.",
          title: audit ? nextAction.title : "Next action waits for Health",
          tone: audit ? "locked" : "idle",
        },
      ]}
    />
  );
}

function HealthAuditRunBoard({
  activeBrainId,
  audit,
  auditError,
  auditRunning,
  issueLimit,
  onIssueLimitChange,
}: {
  activeBrainId: string | null;
  audit: HealthAuditViewModel | null;
  auditError: string | null;
  auditRunning: boolean;
  issueLimit: number;
  onIssueLimitChange: (value: number) => void;
}) {
  const auditState = auditError ? "blocked" : auditRunning ? "running" : audit ? "ready" : "idle";
  const lastAuditText = audit ? `${audit.readiness} / ${audit.alertCount} alert${audit.alertCount === 1 ? "" : "s"}` : "no audit loaded";
  const issueLimitOptions: DropdownOption<string>[] = [4, 8, 12, 16].map((limit) => ({
    value: String(limit),
    label: `Up to ${limit}`,
    meta: "issue samples",
  }));
  return (
    <section className={`health-run-board health-run-board-${auditState}`} aria-label="Health read-only audit setup">
      <div className="health-run-hero">
        <HeartPulse size={18} />
        <div>
          <span>Read-only audit</span>
          <strong>{auditRunning ? "Reading brain-health contract" : audit ? "Audit payload loaded" : "Prepare the Health check"}</strong>
          <p>
            Health reads product readiness, benchmark gates, alerts and recommended preview. It does not apply Sleep, Evolve, Matrix or Grow changes.
          </p>
        </div>
      </div>
      <div className="health-run-grid">
        <article>
          <span>Brain in scope</span>
          <strong title={activeBrainId || undefined}>{activeBrainId ? compactText(activeBrainId, 28) : "unknown"}</strong>
          <p>The selected active brain is the only target for this read.</p>
        </article>
        <div className="health-limit-control health-limit-control-dropdown">
          <Dropdown
            className="health-limit-select"
            disabled={auditRunning}
            label="Diagnostic limit"
            onChange={(value) => onIssueLimitChange(Number(value))}
            options={issueLimitOptions}
            value={String(issueLimit)}
          />
          <p>Controls how many issue samples the audit may return for review.</p>
        </div>
        <article>
          <span>Last payload</span>
          <strong>{lastAuditText}</strong>
          <p>{audit ? healthProductGateDetail(audit) : "Run the audit to load backend proof rows."}</p>
        </article>
        <article>
          <span>Mutation</span>
          <strong>never</strong>
          <p>Any repair remains a separate preview flow with its own guard.</p>
        </article>
      </div>
      {auditError ? (
        <div className="health-run-error">
          <AlertTriangle size={14} />
          <span>Health check blocked</span>
          <strong>{auditError}</strong>
        </div>
      ) : null}
    </section>
  );
}

function healthPagerAutoFocusKey(audit: HealthAuditViewModel | null, auditRunning: boolean, auditError: string | null) {
  if (auditRunning) return "health:running";
  if (auditError) return `health:error:${auditError}`;
  if (audit) return `health:ready:${audit.readiness}:${audit.alertCount}:${audit.checks.length}:${audit.benchmarkPreflight.verdict}`;
  return "health:idle";
}

function healthGuidedStepOverride() {
  if (typeof window === "undefined") return "";
  const value = new URLSearchParams(window.location.search).get("healthStep") || "";
  return ["run", "verdict", "issues", "next"].includes(value) ? value : "";
}

function HealthBrainScene({
  audit,
  auditError,
  auditRunning,
  context,
  overlay,
  vitalState,
}: {
  audit: HealthAuditViewModel | null;
  auditError: string | null;
  auditRunning: boolean;
  context: OpsWorkspaceContext;
  overlay: BrainOpsOverlay;
  vitalState: HealthVitalState;
}) {
  const spatialText = vitalState.hasSpatialTargets ? "spatial alert targets available" : "non-spatial diagnostic";
  const gateText = audit ? healthProductGateDetail(audit) : "not audited";
  const alertText = audit ? `${audit.alertCount} alert${audit.alertCount === 1 ? "" : "s"}` : "not audited";
  const scoreText = vitalState.score === null ? "pending" : `${Math.round(vitalState.score * 100)}%`;
  return (
    <div className={`health-brain-scene health-brain-scene-${vitalState.tone}`}>
      <OpsBrainPreview context={context} overlay={overlay} />
      <div className="health-vital-layer" aria-hidden="true">
        <div className="health-readiness-ring" />
        <div className="health-readiness-pulse" />
        <div className="health-diagnostic-band">
          <span>Alert geometry</span>
          <strong>{spatialText}</strong>
        </div>
      </div>
      <div className="health-vital-readout">
        <span>{auditRunning ? "Audit running" : auditError ? "Audit error" : "Whole-brain readiness"}</span>
        <strong>{auditError ? "blocked" : audit ? vitalState.label : "not audited"}</strong>
        <em>{auditError || `${scoreText} health score / ${alertText} / ${gateText}`}</em>
      </div>
    </div>
  );
}

function HealthReadinessCommandBoard({
  audit,
  auditError,
  auditRunning,
  vitalState,
}: {
  audit: HealthAuditViewModel | null;
  auditError: string | null;
  auditRunning: boolean;
  vitalState: HealthVitalState;
}) {
  const firstAlert = audit ? primaryHealthAlert(audit) : null;
  const decision = healthDecisionCopy(audit, auditError, auditRunning, vitalState);
  const nextAction = healthNextActionCopy(audit);
  const geometryCopy = healthGeometryCopy(audit, vitalState);
  return (
    <section className={`health-command-board health-command-board-${vitalState.tone}`} aria-label="Health readiness command center">
      <div className="health-command-head health-command-head-review">
        <span>Readiness review</span>
        <strong>{decision.shortTitle}</strong>
        <p>{decision.detail}</p>
      </div>
      <div className="health-readiness-review">
        <div className="health-gate-lane">
          <HealthDecisionCard icon={Database} label="Product work" title={decision.productWorkTitle} body={decision.productWorkBody} tone={decision.productWorkTone} />
          <HealthDecisionCard icon={ShieldCheck} label="Certification" title={decision.certificationTitle} body={decision.certificationBody} tone={decision.certificationTone} />
          <HealthDecisionCard icon={AlertTriangle} label="Main warning" title={firstAlert ? healthAlertFamilyLabel(firstAlert.signalFamily) : audit ? "No active warning" : "Waiting for audit"} body={firstAlert ? healthAlertImpactSentence(firstAlert) : "No backend health alert has been loaded yet."} tone={firstAlert ? "blocked" : audit ? "ready" : "idle"} />
        </div>
        <div className="health-next-action-card">
          <div>
            <span>Recommended next preview</span>
            <strong>{nextAction.title}</strong>
            <p>{nextAction.body}</p>
          </div>
          <em>{nextAction.endpointLabel}</em>
        </div>
      </div>
      <dl className="health-command-proof-strip">
        <div>
          <dt>geometry truth</dt>
          <dd>{geometryCopy.title}</dd>
        </div>
        <div>
          <dt>what it draws</dt>
          <dd>{geometryCopy.body}</dd>
        </div>
        <div>
          <dt>mutation</dt>
          <dd>locked / diagnose only</dd>
        </div>
      </dl>
    </section>
  );
}

function HealthDecisionCard({
  body,
  icon: Icon,
  label,
  title,
  tone,
}: {
  body: string;
  icon: LucideIcon;
  label: string;
  title: string;
  tone: "ready" | "warning" | "blocked" | "idle";
}) {
  return (
    <article className={`health-decision-card health-decision-card-${tone}`}>
      <Icon size={15} />
      <span>{label}</span>
      <strong>{title}</strong>
      <p>{body}</p>
    </article>
  );
}

function HealthCheckGrid({ audit }: { audit: HealthAuditViewModel }) {
  return (
    <div className="health-check-grid">
      {audit.checks.map((check) => (
        <article key={check.key}>
          <span>{check.label}</span>
          <strong>{check.score === null ? check.status : `${Math.round(check.score * 100)}%`}</strong>
          <em>{check.summary}</em>
        </article>
      ))}
    </div>
  );
}

type HealthVitalTone = "idle" | "running" | "healthy" | "warning" | "blocked";

type HealthVitalState = {
  hasSpatialTargets: boolean;
  label: string;
  score: number | null;
  tone: HealthVitalTone;
};

function healthVitalState(audit: HealthAuditViewModel | null, running: boolean, error: string | null, hasSpatialTargets: boolean): HealthVitalState {
  if (running) return { hasSpatialTargets, label: "auditing", score: auditHealthScore(audit), tone: "running" };
  if (error) return { hasSpatialTargets, label: "blocked", score: auditHealthScore(audit), tone: "blocked" };
  if (!audit) return { hasSpatialTargets, label: "not audited", score: null, tone: "idle" };
  const score = auditHealthScore(audit);
  if (!audit.benchmarkPreflight.seriousBenchmarkAllowed) return { hasSpatialTargets, label: "benchmark blocked", score, tone: "blocked" };
  if (!audit.benchmarkPreflight.revolutionaryCertificationAllowed || audit.alertCount > 0) return { hasSpatialTargets, label: "watch", score, tone: "warning" };
  return { hasSpatialTargets, label: "healthy", score, tone: "healthy" };
}

function healthGuidedSteps(audit: HealthAuditViewModel | null, running: boolean, error: string | null) {
  return [
    { label: "Prepare", detail: "active brain selected", state: "complete" as const },
    {
      label: "Audit",
      detail: error ? "backend error" : running ? "reading health contract" : audit ? audit.readiness : "run brain-health",
      state: error ? ("blocked" as const) : running ? ("active" as const) : audit ? ("complete" as const) : ("active" as const),
    },
    {
      label: "Inspect",
      detail: audit ? `${audit.alertCount} alerts / ${audit.checks.length} checks` : "waiting for audit",
      state: audit ? ("active" as const) : ("pending" as const),
    },
    {
      label: "Proof / Apply Locked",
      detail: audit ? audit.benchmarkPreflight.verdict : "no mutation path",
      state: audit ? ("complete" as const) : ("pending" as const),
    },
  ];
}

function healthInspectorTone(audit: HealthAuditViewModel | null, running: boolean, error: string | null, vitalState: HealthVitalState): "ready" | "mock" | "blocked" | "live" {
  if (error || vitalState.tone === "blocked") return "blocked";
  if (running) return "live";
  if (audit) return "ready";
  return "mock";
}

function healthInspectorRows(audit: HealthAuditViewModel | null, vitalState: HealthVitalState): Array<[string, string]> {
  const recommendation = audit?.evolutionRecommendation.primaryRecommendation || audit?.recommendation || "none";
  const endpoint = audit?.evolutionRecommendation.endpointHint || "none";
  return [
    ["operation", "diagnose readiness only"],
    ["readiness", audit?.readiness || vitalState.label],
    ["alerts", audit ? String(audit.alertCount) : "not audited"],
    ["benchmark gate", audit ? healthProductGateDetail(audit) : "not audited"],
    ["recommended preview", endpoint === "none" ? healthRecommendationLabel(recommendation) : `${healthRecommendationLabel(recommendation)} / ${endpoint}`],
    ["spatial effect", vitalState.hasSpatialTargets ? "real alert targets" : audit ? "non-spatial diagnostic" : "pending"],
    ["mutation", "none / locked"],
  ];
}

function healthUsabilitySentence(audit: HealthAuditViewModel, vitalState: HealthVitalState) {
  if (!audit.benchmarkPreflight.seriousBenchmarkAllowed) {
    return "Retrieve is usable, but serious benchmark certification is paused until the recommended preview runs.";
  }
  if (vitalState.tone === "warning") {
    return "Usable with warnings: product work can continue, but certification claims need review.";
  }
  return "Usable for the current local product scope; Health did not recommend a repair preview.";
}

function HealthIssueReview({ audit, vitalState }: { audit: HealthAuditViewModel; vitalState: HealthVitalState }) {
  const primaryAlert = primaryHealthAlert(audit);
  const geometryCopy = healthGeometryCopy(audit, vitalState);
  const healthDeltas = healthOperationDeltas(audit);
  const [selectedAlertId, setSelectedAlertId] = useState(primaryAlert?.alertId || audit.alerts[0]?.alertId || "");
  const selectedAlert = audit.alerts.find((alert) => alert.alertId === selectedAlertId) || primaryAlert || audit.alerts[0] || null;
  const selectedDelta = healthDeltas.find((delta) => delta.id === selectedAlert?.alertId) || healthDeltas[0] || null;

  useEffect(() => {
    if (!audit.alerts.length) return;
    if (selectedAlertId && audit.alerts.some((alert) => alert.alertId === selectedAlertId)) return;
    setSelectedAlertId(primaryAlert?.alertId || audit.alerts[0].alertId);
  }, [audit.alerts, primaryAlert, selectedAlertId]);

  if (!audit.alerts.length) {
    return (
      <section className="health-issue-review health-issue-review-clear">
        <CheckCircle2 size={16} />
        <strong>No active Health alerts</strong>
        <p>The backend did not return warnings for this audit. Keep the proof drawer for raw checks if needed.</p>
      </section>
    );
  }

  return (
    <section className="health-issue-review" aria-label="Human-readable health issues">
      {primaryAlert ? (
        <article className="health-priority-issue">
          <span>Priority issue</span>
          <strong>{healthAlertFamilyLabel(primaryAlert.signalFamily)}</strong>
          <p>{healthAlertImpactSentence(primaryAlert)}</p>
          <em>{healthAlertReasonText(primaryAlert)}</em>
        </article>
      ) : null}
      <HealthDeltaReview audit={audit} deltas={healthDeltas} selectedDelta={selectedDelta} />
      <div className="health-issue-review-surface">
        <div className="health-readable-alert-list">
          {audit.alerts.slice(0, 6).map((alert) => {
            const selected = selectedAlert?.alertId === alert.alertId;
            return (
              <button
                className={`health-readable-alert health-readable-alert-${healthAlertTone(alert)}${selected ? " selected" : ""}`}
                key={alert.alertId}
                onClick={() => setSelectedAlertId(alert.alertId)}
                type="button"
              >
                <span>{healthSeverityLabel(alert.severity)}</span>
                <strong>{healthAlertFamilyLabel(alert.signalFamily)}</strong>
                <p>{healthAlertImpactSentence(alert)}</p>
                <em>{healthAlertReasonText(alert)}</em>
                {alert.endpointHint ? <small>{alert.endpointHint}</small> : null}
              </button>
            );
          })}
        </div>
        <HealthAlertInspector alert={selectedAlert} audit={audit} geometryCopy={geometryCopy} vitalState={vitalState} />
      </div>
      <div className="health-geometry-truth">
        <span>Geometry truth</span>
        <strong>{geometryCopy.title}</strong>
        <p>{geometryCopy.body}</p>
      </div>
    </section>
  );
}

function HealthDeltaReview({
  audit,
  deltas,
  selectedDelta,
}: {
  audit: HealthAuditViewModel;
  deltas: OperationDelta[];
  selectedDelta: OperationDelta | null;
}) {
  const batchBase = operationDeltaBatchReview(deltas, {
    emptyTitle: "No diagnostic deltas",
    focusedDeltaId: selectedDelta?.id || null,
    operationLabel: "health",
    previewLoaded: true,
  });
  const truth = summarizeOperationDeltas(deltas);
  const endpointCount = audit.alerts.filter((alert) => alert.endpointHint).length;
  const diagnosticItems = batchBase.items.map((item) => {
    const delta = deltas.find((candidate) => candidate.id === item.id);
    const renderState = delta ? operationDeltaRenderableState(delta) : "ledger_only";
    return {
      ...item,
      tone: renderState === "coordinate" || renderState === "target_ids" ? ("included" as const) : item.tone,
      value: renderState === "ledger_only" ? "diagnostic ledger / no geometry" : item.value,
    };
  });
  return (
    <div className="health-delta-review" aria-label="Health diagnostic delta review">
      <ReviewBatchDeltaMap
        items={diagnosticItems}
        stats={[
          { detail: "backend health alerts", label: "Signals", tone: deltas.length ? "ready" : "muted", value: String(deltas.length) },
          { detail: "safe to highlight in brain", label: "Spatial", tone: truth.renderableCount ? "ready" : "muted", value: String(truth.renderableCount) },
          { detail: "kept in review without fake points", label: "Ledger-only", tone: truth.ledgerOnlyCount ? "selected" : "muted", value: String(truth.ledgerOnlyCount) },
          { detail: "recommended next preview route", label: "Endpoints", tone: endpointCount ? "ready" : "muted", value: String(endpointCount) },
        ]}
        subtitle="Health diagnostics use the same delta truth model as Grow and Maintain, but remain read-only: no alert is applied from this page."
        title={`${deltas.length} health diagnostic signal${deltas.length === 1 ? "" : "s"}`}
      />
      {selectedDelta ? (
        <ReviewItemImpactPanel
          steps={operationDeltaImpactSteps({
            applyDetail: "Health never mutates memory. Use the recommended preview route before any guarded apply flow.",
            applyLabel: "Mutation",
            applyValue: "locked",
            delta: selectedDelta,
            geometryDetail:
              operationDeltaRenderableState(selectedDelta) === "ledger_only"
                ? "The backend alert has no target ids or coordinates, so the brain view must not invent a region."
                : "The backend alert includes target ids or coordinates that can be highlighted when the current graph sample contains them.",
            geometryLabel: "Diagnostic geometry",
            sourceDetail: selectedDelta.evidenceRefs.length ? selectedDelta.evidenceRefs.map((ref) => ref.label).join(" / ") : selectedDelta.reason,
            sourceLabel: "Health evidence",
          })}
          subtitle="Selected alert impact, mapped through the shared delta contract."
          title={selectedDelta.label || "Health signal"}
        />
      ) : null}
      <ReviewDecisionLedger
        items={[
          { detail: `${audit.checks.length} backend checks normalized`, label: "Checks", tone: "ready", value: String(audit.checks.length) },
          { detail: `${audit.alertCount} alert contracts loaded`, label: "Alerts", tone: audit.alertCount ? "selected" : "ready", value: String(audit.alertCount) },
          {
            detail: truth.renderableCount ? "some alerts can map to graph targets" : "no alert can be drawn as spatial truth",
            label: "Render truth",
            tone: truth.renderableCount ? "ready" : "muted",
            value: truth.renderableCount ? "spatial" : "ledger",
          },
          {
            detail: audit.evolutionRecommendation.endpointHint || "Health returned no repair endpoint",
            label: "Next preview",
            tone: audit.evolutionRecommendation.endpointHint ? "selected" : "muted",
            value: healthRecommendationLabel(audit.evolutionRecommendation.primaryRecommendation || audit.recommendation),
          },
          { detail: "Health is read-only; apply belongs to Grow, Sleep, Evolve or Matrix.", label: "Mutation", tone: "blocked", value: "locked" },
        ]}
        routeDetail={audit.evolutionRecommendation.endpointHint || "No preview route recommended by this Health response."}
        routeLabel={audit.evolutionRecommendation.endpointHint ? "route available" : "diagnose only"}
        summary="Health separates diagnostic truth from mutation. Renderable alerts can be highlighted; ledger-only alerts stay visible without fake geometry."
        title="Health truth ledger"
        tone={audit.evolutionRecommendation.endpointHint ? "selected" : "ready"}
      />
    </div>
  );
}

function HealthAlertInspector({
  alert,
  audit,
  geometryCopy,
  vitalState,
}: {
  alert: BrainHealthAlertView | null;
  audit: HealthAuditViewModel;
  geometryCopy: { body: string; title: string };
  vitalState: HealthVitalState;
}) {
  if (!alert) {
    return (
      <section className="health-alert-inspector" aria-label="Selected health alert inspector">
        <header>
          <span>Selected alert</span>
          <strong>No alert selected</strong>
          <p>The backend returned no alert to inspect.</p>
        </header>
      </section>
    );
  }
  const nextAction = healthNextActionCopy(audit);
  return (
    <section className={`health-alert-inspector health-alert-inspector-${healthAlertTone(alert)}`} aria-label="Selected health alert inspector">
      <header>
        <span>Selected alert</span>
        <strong>{healthAlertFamilyLabel(alert.signalFamily)}</strong>
        <p>{healthAlertImpactSentence(alert)}</p>
        <em>{healthSeverityLabel(alert.severity)}</em>
      </header>
      <DefinitionRows rows={healthAlertInspectorRows(alert, audit, nextAction, vitalState)} />
      <div className="health-alert-reason-pills" aria-label="Health alert reason codes">
        {alert.reasonCodes.length ? alert.reasonCodes.map((reason) => <span key={reason}>{reason.replace(/^[a-z_]+:/, "").replace(/_/g, " ")}</span>) : <span>no reason codes</span>}
      </div>
      <div className="health-alert-geometry-receipt">
        <span>Brain effect truth</span>
        <strong>{geometryCopy.title}</strong>
        <p>{geometryCopy.body}</p>
      </div>
    </section>
  );
}

function healthAlertInspectorRows(
  alert: BrainHealthAlertView,
  audit: HealthAuditViewModel,
  nextAction: ReturnType<typeof healthNextActionCopy>,
  vitalState: HealthVitalState,
): Array<[string, string]> {
  return [
    ["alert id", alert.alertId],
    ["severity", healthSeverityLabel(alert.severity)],
    ["impact", alert.impact ? alert.impact.replace(/_/g, " ") : "informational"],
    ["backend family", alert.signalFamily.replace(/_/g, " ")],
    ["recommended preview", nextAction.title],
    ["endpoint", alert.endpointHint || nextAction.endpointLabel || "none"],
    ["health score", vitalState.score === null ? "pending" : `${Math.round(vitalState.score * 100)}%`],
    ["benchmark gate", audit.benchmarkPreflight.seriousBenchmarkAllowed ? "allowed" : "paused"],
  ];
}

function HealthNextActionPanel({
  audit,
  onOpenGrow,
  onOpenMaintain,
}: {
  audit: HealthAuditViewModel | null;
  onOpenGrow?: () => void;
  onOpenMaintain?: (mode?: "sleep" | "evolve" | "matrix") => void;
}) {
  const nextAction = healthNextActionCopy(audit);
  const primaryAlert = audit ? primaryHealthAlert(audit) : null;
  const route = healthNextRoute(nextAction.endpointLabel, nextAction.title);
  const canOpenRoute = route.target === "grow" ? Boolean(onOpenGrow) : route.target === "maintain" ? Boolean(onOpenMaintain) : false;
  const certificationLabel = audit
    ? audit.benchmarkPreflight.revolutionaryCertificationAllowed
      ? "Certification open"
      : audit.benchmarkPreflight.seriousBenchmarkAllowed
        ? "Warning scope"
        : "Certification paused"
    : "Not audited";
  const previewRequired = Boolean(audit && nextAction.endpointLabel !== "none");
  const openRoute = () => {
    if (route.target === "grow") onOpenGrow?.();
    if (route.target === "maintain") onOpenMaintain?.(route.maintenanceMode);
  };
  return (
    <section className={`health-next-action-panel health-next-action-panel-${audit ? "ready" : "idle"}`} aria-label="Health next action">
      <div className="health-next-action-panel-head">
        <ShieldCheck size={16} />
        <div>
          <span>{previewRequired ? "Operator handoff" : "Operator action"}</span>
          <strong>{nextAction.title}</strong>
          <p>{nextAction.body}</p>
        </div>
        <em>{certificationLabel}</em>
      </div>
      <div className={`health-next-route-card health-next-route-card-${route.target}`}>
        <div>
          <span>Recommended route</span>
          <strong>{route.label}</strong>
          <p>{route.detail}</p>
        </div>
        <button disabled={!canOpenRoute} onClick={openRoute} title={canOpenRoute ? `Open ${route.label}` : route.disabledReason} type="button">
          <ArrowRight size={14} />
          <span>{canOpenRoute ? `Open ${route.shortLabel}` : route.disabledReason}</span>
        </button>
      </div>
      <div className="health-next-action-decision">
        <article>
          <span>Why</span>
          <strong>{primaryAlert ? healthAlertFamilyLabel(primaryAlert.signalFamily) : audit ? "No active repair signal" : "Audit required"}</strong>
          <p>{primaryAlert ? healthAlertImpactSentence(primaryAlert) : audit ? "The backend did not request a repair preview for this Health payload." : "Run the read-only audit before choosing an operation."}</p>
        </article>
        <article>
          <span>Safety</span>
          <strong>{previewRequired ? "Preview only" : "No mutation"}</strong>
          <p>{previewRequired ? "Open the suggested operation as a bounded preview. Do not apply from Health." : "Health has no apply path and does not mutate the brain."}</p>
        </article>
      </div>
      <div className="health-next-action-steps">
        <article>
          <span>1</span>
          <strong>{previewRequired ? `Open ${nextAction.title}` : "Keep current brain"}</strong>
          <p>{previewRequired ? "Use the corresponding Grow or Maintain page; Health only hands off the recommendation." : "No repair preview is required by this audit."}</p>
        </article>
        <article>
          <span>2</span>
          <strong>Review returned evidence</strong>
          <p>Inspect ids, coordinates, proposals and blockers in that operation before considering any guarded apply path.</p>
        </article>
        <article>
          <span>3</span>
          <strong>Re-run Health</strong>
          <p>Only a fresh Health audit can reopen serious benchmark or certification claims.</p>
        </article>
      </div>
      <div className="health-next-endpoint">
        <span>MCP endpoint</span>
        <strong>{nextAction.endpointLabel}</strong>
      </div>
    </section>
  );
}

function auditHealthScore(audit: HealthAuditViewModel | null) {
  if (!audit) return null;
  if (audit.score !== null) return audit.score;
  const scores = audit.checks.map((check) => check.score).filter((score): score is number => score !== null);
  if (!scores.length) return null;
  return scores.reduce((sum, score) => sum + score, 0) / scores.length;
}

function healthDecisionCopy(audit: HealthAuditViewModel | null, auditError: string | null, auditRunning: boolean, vitalState: HealthVitalState) {
  const validationGate = audit ? healthValidationGate(audit) : null;
  const nextAction = healthNextActionCopy(audit);

  if (auditError) {
    return {
      body: "The Health tool did not return a valid audit. Keep the brain out of product claims until the error is resolved.",
      certificationBody: "No certification claim is safe without a valid Health payload.",
      certificationTitle: "Blocked",
      certificationTone: "blocked" as const,
      detail: auditError,
      productWorkBody: "Use only basic diagnostics until the audit runs cleanly.",
      productWorkTitle: "Diagnostic only",
      productWorkTone: "blocked" as const,
      shortTitle: "Audit blocked",
      title: "Audit blocked",
      tone: "blocked" as const,
    };
  }

  if (auditRunning) {
    return {
      body: "Reading the backend readiness contract, recent retrieval learning signals and benchmark gate.",
      certificationBody: "Waiting for the backend verdict.",
      certificationTitle: "Checking",
      certificationTone: "idle" as const,
      detail: "No claim is updated until the audit payload returns.",
      productWorkBody: "Keep the current brain state unchanged while Health reads it.",
      productWorkTitle: "Checking",
      productWorkTone: "idle" as const,
      shortTitle: "Auditing",
      title: "Auditing brain health",
      tone: "running" as const,
    };
  }

  if (!audit) {
    return {
      body: "Run the read-only Health audit to know whether this brain is usable, watched or blocked.",
      certificationBody: "Not audited yet.",
      certificationTitle: "Unknown",
      certificationTone: "idle" as const,
      detail: "No readiness payload loaded.",
      productWorkBody: "No product decision has been loaded.",
      productWorkTitle: "Unknown",
      productWorkTone: "idle" as const,
      shortTitle: "Not audited",
      title: "Run Health to get a verdict",
      tone: "idle" as const,
    };
  }

  const retrieveUsable = validationGate?.currentBrainUsable !== false;
  const certificationAllowed = audit.benchmarkPreflight.revolutionaryCertificationAllowed;
  const seriousAllowed = audit.benchmarkPreflight.seriousBenchmarkAllowed;
  const title = healthUsabilitySentence(audit, vitalState);

  if (!seriousAllowed) {
    return {
      body: `${nextAction.title} is required before serious benchmark or external certification claims. Normal diagnostic and retrieve work can continue if the validation gate stays usable.`,
      certificationBody: "Paused until the recommended preview is run and Health is green again.",
      certificationTitle: "Preview required",
      certificationTone: "blocked" as const,
      detail: `Health score ${Math.round((vitalState.score || 0) * 100)}%, ${audit.alertCount} alert${audit.alertCount === 1 ? "" : "s"}.`,
      productWorkBody: retrieveUsable ? "The validation gate says the brain is still usable for retrieve/product iteration." : "The validation gate did not clear normal product use.",
      productWorkTitle: retrieveUsable ? "Allowed with guard" : "Blocked",
      productWorkTone: retrieveUsable ? ("warning" as const) : ("blocked" as const),
      shortTitle: "Needs preview before certification",
      title,
      tone: "warning" as const,
    };
  }

  if (!certificationAllowed || audit.alertCount > 0) {
    return {
      body: "Product work can continue, but certification language should stay conservative until the watch items are cleared.",
      certificationBody: "Allowed only with explicit warning scope.",
      certificationTitle: "Warning scope",
      certificationTone: "warning" as const,
      detail: `Health score ${Math.round((vitalState.score || 0) * 100)}%, ${audit.alertCount} watch item${audit.alertCount === 1 ? "" : "s"}.`,
      productWorkBody: "The brain can be used for local product iteration.",
      productWorkTitle: "Allowed",
      productWorkTone: "ready" as const,
      shortTitle: "Usable with warnings",
      title,
      tone: "warning" as const,
    };
  }

  return {
    body: "No Health warning currently blocks the local product scope. Keep the exact audit artifact with benchmark evidence.",
    certificationBody: "Allowed for the audited scope.",
    certificationTitle: "Allowed",
    certificationTone: "ready" as const,
    detail: `Health score ${Math.round((vitalState.score || 0) * 100)}%, all main gates clear.`,
    productWorkBody: "The active brain is usable for the current local product scope.",
    productWorkTitle: "Allowed",
    productWorkTone: "ready" as const,
    shortTitle: "Ready for audited scope",
    title,
    tone: "healthy" as const,
  };
}

function healthNextActionCopy(audit: HealthAuditViewModel | null) {
  if (!audit) {
    return {
      body: "Health has not returned a recommendation yet.",
      endpointLabel: "none",
      title: "Run Health first",
    };
  }
  const recommendation = audit.evolutionRecommendation.primaryRecommendation || audit.recommendation || "none";
  const endpoint = audit.evolutionRecommendation.endpointHint || "none";
  const title = healthRecommendationLabel(recommendation);
  if (endpoint === "none" || recommendation === "none") {
    return {
      body: "The backend did not request a Sleep, Evolve, Matrix or Grow preview for this audit.",
      endpointLabel: "none",
      title: "No preview recommended",
    };
  }
  return {
    body: `${title} should be run as a non-mutating preview, then Health should be run again before serious benchmark claims.`,
    endpointLabel: endpoint,
    title,
  };
}

function healthNextRoute(endpointLabel: string, title: string) {
  const normalized = `${endpointLabel} ${title}`.toLowerCase();
  if (normalized.includes("grow")) {
    return {
      detail: "Open Grow to run the recommended source preview with operator review before any guarded apply.",
      disabledReason: "Grow route unavailable",
      label: "Grow Workspace",
      shortLabel: "Grow",
      target: "grow" as const,
    };
  }
  if (normalized.includes("matrix")) {
    return {
      detail: "Open Maintain directly on Matrix so the operator can run the recommended calibration preview.",
      disabledReason: "Maintain route unavailable",
      label: "Matrix Calibration",
      maintenanceMode: "matrix" as const,
      shortLabel: "Matrix",
      target: "maintain" as const,
    };
  }
  if (normalized.includes("evolve")) {
    return {
      detail: "Open Maintain directly on Evolve so the operator can run the recommended topology preview.",
      disabledReason: "Maintain route unavailable",
      label: "Evolve Topology",
      maintenanceMode: "evolve" as const,
      shortLabel: "Evolve",
      target: "maintain" as const,
    };
  }
  if (normalized.includes("sleep") || normalized.includes("maintain")) {
    return {
      detail: "Open Maintain directly on Sleep so the operator can run the recommended consolidation preview.",
      disabledReason: "Maintain route unavailable",
      label: "Sleep Consolidation",
      maintenanceMode: "sleep" as const,
      shortLabel: "Sleep",
      target: "maintain" as const,
    };
  }
  return {
    detail: "Health did not request an operation handoff for this audit.",
    disabledReason: "No route needed",
    label: "No operation handoff",
    maintenanceMode: undefined,
    shortLabel: "Route",
    target: "none" as const,
  };
}

function healthGeometryCopy(audit: HealthAuditViewModel | null, vitalState: HealthVitalState) {
  if (!audit) {
    return {
      body: "No backend Health audit has been loaded.",
      title: "Pending",
    };
  }
  if (vitalState.hasSpatialTargets) {
    return {
      body: "The Health payload provided real ids or coordinates, so the brain can highlight targeted regions.",
      title: "Targeted Health geometry",
    };
  }
  return {
    body: "The backend returned whole-brain diagnostic signals only. The UI shows global readiness and does not invent affected regions.",
    title: "Whole-brain diagnostic",
  };
}

function healthStatusSummary(audit: HealthAuditViewModel, vitalState: HealthVitalState) {
  const next = healthNextActionCopy(audit);
  if (!audit.benchmarkPreflight.seriousBenchmarkAllowed) return `Needs preview / ${next.title}`;
  if (vitalState.tone === "warning") return `Watch / ${next.title}`;
  return `Healthy / ${next.title}`;
}

function healthProductGateDetail(audit: HealthAuditViewModel) {
  if (audit.benchmarkPreflight.revolutionaryCertificationAllowed) return "Certification allowed for audited scope";
  if (audit.benchmarkPreflight.seriousBenchmarkAllowed) return "Benchmark allowed with warning scope";
  return "Preview required before serious benchmark";
}

function healthRecommendationLabel(value: string) {
  const normalized = value.toLowerCase();
  if (!normalized || normalized === "none") return "No preview recommended";
  if (normalized.includes("matrix")) return "Matrix calibration preview";
  if (normalized.includes("evolve")) return "Evolve topology preview";
  if (normalized.includes("sleep")) return "Sleep consolidation preview";
  if (normalized.includes("grow")) return "Grow source preview";
  if (normalized.includes("watch")) return "Watch for more evidence";
  return value.replace(/_/g, " ");
}

function primaryHealthAlert(audit: HealthAuditViewModel) {
  return (
    audit.alerts.find((alert) => alert.severity === "action_recommended") ||
    audit.alerts.find((alert) => alert.endpointHint) ||
    audit.alerts[0] ||
    null
  );
}

function healthAlertTone(alert: BrainHealthAlertView): "ready" | "warning" | "blocked" {
  if (alert.severity === "action_recommended" || alert.endpointHint) return "blocked";
  if (alert.severity === "watch") return "warning";
  return "ready";
}

function healthSeverityLabel(value: string) {
  if (value === "action_recommended") return "Action recommended";
  if (value === "watch") return "Watch";
  if (!value) return "Signal";
  return value.replace(/_/g, " ");
}

function healthAlertFamilyLabel(value: string) {
  if (value === "ai_spatial") return "AI spatial watch";
  if (value === "context_quality") return "Repeated context quality warning";
  if (value === "heuristic_learning") return "Spatial correction review";
  if (value === "source_document") return "Document retrieval signal";
  if (!value) return "Health signal";
  return value.replace(/_/g, " ");
}

function healthAlertImpactSentence(alert: BrainHealthAlertView) {
  if (alert.impact === "requires_preview_before_serious_benchmark") {
    return "A non-mutating preview is required before serious benchmark or certification claims.";
  }
  if (alert.impact === "benchmark_allowed_with_warning") {
    return "This is a watch signal: keep the warning visible, but it does not alone block normal retrieve work.";
  }
  if (alert.impact === "benchmark_blocked_until_preview") {
    return "Benchmark claims are blocked until the recommended preview is reviewed.";
  }
  if (alert.impact) return alert.impact.replace(/_/g, " ");
  return "Backend returned this diagnostic signal without a detailed impact sentence.";
}

function healthAlertReasonText(alert: BrainHealthAlertView) {
  const reasons = alert.reasonCodes.map((reason) => reason.replace(/^[a-z_]+:/, "").replace(/_/g, " "));
  return reasons.length ? compactText(reasons.join(" / "), 140) : "No reason code returned.";
}

function healthValidationGate(audit: HealthAuditViewModel) {
  const gate = recordValue(audit.original.validation_brain_rebuild_gate);
  if (!gate) return null;
  return {
    currentBrainUsable: booleanValue(gate.current_brain_usable_for_retrieve_benchmarks),
    productProofAllowed: booleanValue(gate.product_proof_allowed_by_this_gate),
    status: typeof gate.status === "string" ? gate.status : "",
  };
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function HealthAlertList({ audit }: { audit: HealthAuditViewModel }) {
  if (!audit.alerts.length) return <p className="mode-empty-copy">No health alerts returned by the backend.</p>;
  return (
    <div className="health-alert-list">
      {audit.alerts.slice(0, 8).map((alert) => (
        <article key={alert.alertId}>
          <span>{alert.severity}</span>
          <strong>{alert.signalFamily}</strong>
          <em>{alert.reasonCodes.join(", ") || alert.impact}</em>
          {alert.endpointHint ? <small>{alert.endpointHint}</small> : null}
        </article>
      ))}
    </div>
  );
}
