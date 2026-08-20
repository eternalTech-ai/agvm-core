import { ArrowRight, CheckCircle2, ChevronLeft, ChevronRight, CircleDashed, Database, Lock, RefreshCw, ShieldCheck, XCircle, type LucideIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReactNode, Ref } from "react";

import type { BrainRenderNode } from "../brain/brainSceneModel";
import { ProductPageFrame } from "../shell/ProductPageFrame";
import { compactId, compactNodeEvidence, compactText } from "./opsFormatters";
import type { GuardedAction, ModeMetric, OpsModeKey, OpsWorkspaceContext } from "./opsWorkspaceTypes";

export type MutationGuardRequirementState = "ready" | "missing" | "blocked" | "locked";

export type MutationGuardRequirement = {
  detail?: string;
  label: string;
  state: MutationGuardRequirementState;
  value: string;
};

export type MutationGuardView = {
  actionLabel?: string;
  blockedReasons: string[];
  endpoint: string;
  requirements: MutationGuardRequirement[];
  rows: Array<[string, string]>;
  status: string;
  summary: string;
  tone: "ready" | "mock" | "blocked";
};

export type MutationGuardProofView = {
  actionLabel: string;
  canRun: boolean;
  disabledReason: string;
  onRun: () => void;
  rows: Array<[string, string]>;
  running: boolean;
  status: string;
  summary: string;
  tone: "ready" | "mock" | "blocked";
};

export type MutationApplyExecutionView = {
  actionLabel: string;
  blockedReasons: string[];
  canExecute: boolean;
  confirmationPhrase: string;
  confirmationValue: string;
  disabledReason: string;
  endpoint: string;
  operatorControls?: "hidden" | "visible";
  onConfirmationChange: (value: string) => void;
  onExecute?: () => void;
  onRollbackConsentChange: (value: boolean) => void;
  requirements: MutationGuardRequirement[];
  rollbackConsent: boolean;
  rows: Array<[string, string]>;
  running: boolean;
  status: string;
  summary: string;
  tone: "ready" | "mock" | "blocked";
};

export type GuidedOperationStepState = "complete" | "active" | "pending" | "blocked";

export type GuidedOperationStep = {
  detail?: string;
  label: string;
  state: GuidedOperationStepState;
};

export type GuidedProofTab = {
  content: ReactNode;
  id: string;
  label: string;
  tone?: "ready" | "mock" | "blocked";
};

export type GuidedInspectorTone = "ready" | "mock" | "blocked" | "live";

export type GuidedDecisionPageTone = "idle" | "live" | "ready" | "blocked" | "locked";

export type GuidedDecisionPage = {
  action?: ReactNode;
  blockedReason?: string;
  canEnter?: boolean;
  content?: ReactNode;
  id: string;
  isComplete?: boolean;
  kicker: string;
  label: string;
  primaryAction?: ReactNode;
  rows?: Array<[string, string]>;
  statusLabel?: string;
  summary: string;
  title: string;
  tone?: GuidedDecisionPageTone;
};

export type ReviewDecisionLedgerItem = {
  detail: string;
  label: string;
  tone: "ready" | "selected" | "muted" | "blocked";
  value: string;
};

export type ReviewItemImpactStep = {
  detail: string;
  label: string;
  tone: "ready" | "selected" | "muted" | "blocked";
  value: string;
};

export type ReviewBatchDeltaTone = "backend" | "included" | "excluded" | "moving" | "review" | "muted";

export type ReviewBatchDeltaItem = {
  detail: string;
  focused?: boolean;
  id: string;
  label: string;
  tone: ReviewBatchDeltaTone;
  value: string;
};

export type ReviewBatchDeltaStat = {
  detail: string;
  label: string;
  tone: "ready" | "selected" | "muted" | "blocked";
  value: string;
};

export function BrainFirstWorkspace({
  actions,
  children,
  embedded = false,
  eyebrow,
  icon: Icon,
  intent,
  metrics,
  mode,
  status,
  title,
}: {
  actions: GuardedAction[];
  children: ReactNode;
  embedded?: boolean;
  eyebrow: string;
  icon: LucideIcon;
  intent: string;
  metrics: ModeMetric[];
  mode: OpsModeKey;
  status: string;
  title: string;
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bodyRef.current?.scrollTo({ left: 0, top: 0 });
  }, [mode, status]);

  return (
    <ProductPageFrame
      actions={actions}
      bodyRef={bodyRef}
      chrome={embedded ? "embedded" : "full"}
      className="brain-first-page-frame"
      eyebrow={eyebrow}
      icon={Icon}
      intent={intent}
      metrics={metrics}
      mode={mode}
      status={status}
      title={title}
    >
      {children}
    </ProductPageFrame>
  );
}

export function EvidenceDrawer({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`brain-first-evidence mode-workbench ${className}`.trim()}>{children}</section>;
}

export function GuidedInspectorPanel({
  eyebrow,
  rows,
  summary,
  title,
  tone = "mock",
}: {
  eyebrow: string;
  rows: Array<[string, string]>;
  summary: string;
  title: string;
  tone?: GuidedInspectorTone;
}) {
  return (
    <section className={`guided-inspector guided-inspector-${tone}`} aria-label={`${title} inspector`}>
      <div className="guided-inspector-head">
        <span>{eyebrow}</span>
        <strong>{title}</strong>
        <p>{summary}</p>
      </div>
      <DefinitionRows rows={rows} />
    </section>
  );
}

export function GuidedDecisionPager({
  ariaLabel,
  autoFocusKey,
  autoFocusPageId,
  pages,
}: {
  ariaLabel: string;
  autoFocusKey?: string;
  autoFocusPageId?: string;
  pages: GuidedDecisionPage[];
}) {
  const [pageIndex, setPageIndex] = useState(0);
  const lastAutoFocusKeyRef = useRef<string | null>(null);
  const pageSignature = pages.map((item) => item.id).join("|");
  const safeIndex = enterablePageIndex(pages, Math.min(pageIndex, Math.max(0, pages.length - 1)));
  const page = pages[safeIndex];
  const pageBodyRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (pageIndex >= pages.length) setPageIndex(Math.max(0, pages.length - 1));
  }, [pageIndex, pages.length]);

  useEffect(() => {
    const nextIndex = enterablePageIndex(pages, Math.min(pageIndex, Math.max(0, pages.length - 1)));
    if (nextIndex !== pageIndex) setPageIndex(nextIndex);
  }, [pageIndex, pages]);

  useEffect(() => {
    if (!autoFocusPageId) return;
    const nextIndex = pages.findIndex((item) => item.id === autoFocusPageId);
    if (nextIndex < 0) return;
    const focusKey = autoFocusKey || `${pageSignature}:${autoFocusPageId}`;
    if (lastAutoFocusKeyRef.current === focusKey) return;
    lastAutoFocusKeyRef.current = focusKey;
    setPageIndex(enterablePageIndex(pages, nextIndex));
  }, [autoFocusKey, autoFocusPageId, pageSignature, pages]);

  useEffect(() => {
    if (!pageBodyRef.current) return;
    pageBodyRef.current.scrollTop = 0;
  }, [page?.id]);

  if (!page) return null;

  const previousDisabled = safeIndex === 0;
  const currentComplete = page.isComplete ?? true;
  const nextIndex = safeIndex + 1;
  const nextPage = pages[nextIndex];
  const nextDisabled = safeIndex === pages.length - 1 || !currentComplete || !isPageEnterable(nextPage);
  const stepStatus = page.statusLabel || page.blockedReason || (currentComplete ? "Step complete" : "Step not complete");
  const nextDisabledReason = nextActionDisabledReason(page, nextPage, currentComplete, safeIndex === pages.length - 1);

  return (
    <section className={`guided-decision-pager guided-decision-pager-${page.tone || "idle"}`} aria-label={ariaLabel}>
      <header className="guided-decision-pager-head">
        <div>
          <span>{page.kicker}</span>
          <strong>{page.title}</strong>
          <p>{page.summary}</p>
        </div>
        <em>{safeIndex + 1}/{pages.length}</em>
      </header>

      <div className="guided-decision-track" role="tablist" aria-label={`${ariaLabel} steps`}>
        {pages.map((item, index) => {
          const enterable = isPageEnterable(item);
          const complete = item.isComplete ?? false;
          const stateClass = enterable ? complete ? "complete" : "available" : "locked";
          return (
            <button
              aria-disabled={!enterable}
              aria-selected={index === safeIndex}
              className={`${index === safeIndex ? "active " : ""}guided-decision-track-${item.tone || "idle"} guided-decision-track-state-${stateClass}`}
              disabled={!enterable}
              key={item.id}
              onClick={() => {
                if (enterable) setPageIndex(index);
              }}
              role="tab"
              title={enterable ? item.statusLabel || item.title : item.blockedReason || "Complete previous step first"}
              type="button"
            >
              <span>{index + 1}</span>
              <strong>{item.label}</strong>
            </button>
          );
        })}
      </div>

      <div className="guided-decision-page-body" ref={pageBodyRef}>
        {page.rows?.length ? <DefinitionRows rows={page.rows} /> : null}
        {page.content}
      </div>

      {page.action ? <div className="guided-decision-action">{page.action}</div> : null}

      <PrimaryActionBar
        currentStatus={stepStatus}
        disabledReason={nextDisabledReason}
        nextDisabled={nextDisabled}
        onNext={() => setPageIndex(nextIndex)}
        onPrevious={() => setPageIndex((current) => Math.max(0, current - 1))}
        previousDisabled={previousDisabled}
        primaryAction={page.primaryAction}
      />
    </section>
  );
}

export function PrimaryActionBar({
  currentStatus,
  disabledReason,
  nextDisabled,
  onNext,
  onPrevious,
  previousDisabled,
  primaryAction,
}: {
  currentStatus: string;
  disabledReason?: string;
  nextDisabled: boolean;
  onNext: () => void;
  onPrevious: () => void;
  previousDisabled: boolean;
  primaryAction?: ReactNode;
}) {
  return (
    <footer className="guided-primary-action-bar">
      <button className="guided-primary-action-nav" disabled={previousDisabled} onClick={onPrevious} title="Previous step" type="button">
        <ChevronLeft size={15} />
        <span>Back</span>
      </button>
      <div className="guided-primary-action-status" aria-live="polite">
        <span>Current step</span>
        <strong>{currentStatus}</strong>
        {disabledReason ? <em>{disabledReason}</em> : null}
      </div>
      <div className={primaryAction ? "guided-primary-action-slot" : "guided-primary-action-slot guided-primary-action-slot-empty"}>
        {primaryAction || (
          <span>
            <Lock size={14} />
            No action required
          </span>
        )}
      </div>
      <button className="guided-primary-action-nav" disabled={nextDisabled} onClick={onNext} title={disabledReason || "Next step"} type="button">
        <span>Next</span>
        <ChevronRight size={15} />
      </button>
    </footer>
  );
}

function nextActionDisabledReason(page: GuidedDecisionPage, nextPage: GuidedDecisionPage | undefined, currentComplete: boolean, lastPage: boolean) {
  if (lastPage) return "Final step";
  if (!currentComplete) return page.blockedReason || "Complete this step before continuing";
  if (!isPageEnterable(nextPage)) return nextPage?.blockedReason || "Complete previous step first";
  return undefined;
}

function isPageEnterable(page: GuidedDecisionPage | undefined) {
  if (!page) return false;
  return page.canEnter ?? true;
}

function enterablePageIndex(pages: GuidedDecisionPage[], requestedIndex: number) {
  if (!pages.length) return 0;
  const boundedIndex = Math.min(Math.max(0, requestedIndex), pages.length - 1);
  if (isPageEnterable(pages[boundedIndex])) return boundedIndex;
  for (let index = boundedIndex - 1; index >= 0; index -= 1) {
    if (isPageEnterable(pages[index])) return index;
  }
  const firstEnterable = pages.findIndex(isPageEnterable);
  return firstEnterable >= 0 ? firstEnterable : 0;
}

export function MutationGuardPanel({ guard, title = "Mutation Guard" }: { guard: MutationGuardView; title?: string }) {
  return (
    <ContractPanel title={title} tone={guard.tone} icon={Lock}>
      <section className={`mutation-guard-panel mutation-guard-panel-${guard.tone}`}>
        <div className="mutation-guard-head">
          <span>{guard.endpoint}</span>
          <strong>{guard.status}</strong>
          <p>{guard.summary}</p>
        </div>
        <div className="mutation-requirement-grid" aria-label={`${title} requirements`}>
          {guard.requirements.map((requirement) => (
            <article className={`mutation-requirement mutation-requirement-${requirement.state}`} key={requirement.label}>
              <span>{requirement.label}</span>
              <strong>{requirement.value}</strong>
              {requirement.detail ? <em>{requirement.detail}</em> : null}
            </article>
          ))}
        </div>
        {guard.blockedReasons.length ? (
          <div className="mutation-guard-blockers">
            <span>Blocked reasons</span>
            <strong>{guard.blockedReasons.join(", ")}</strong>
          </div>
        ) : null}
        <DefinitionRows rows={guard.rows} />
        <button className="mutation-guard-apply" disabled type="button">
          <Lock size={14} />
          <span>{guard.actionLabel || "Apply locked"}</span>
        </button>
      </section>
    </ContractPanel>
  );
}

export function MutationGuardProofPanel({ proof, title = "Apply Regression Proof" }: { proof: MutationGuardProofView; title?: string }) {
  return (
    <ContractPanel title={title} tone={proof.tone} icon={ShieldCheck}>
      <section className={`mutation-proof-panel mutation-proof-panel-${proof.tone}`}>
        <div className="mutation-proof-head">
          <span>OPS-5B blocked apply probe</span>
          <strong>{proof.status}</strong>
          <p>{proof.summary}</p>
        </div>
        <DefinitionRows rows={proof.rows} />
        <button className="mutation-proof-run" disabled={!proof.canRun || proof.running} onClick={proof.onRun} type="button">
          {proof.running ? <RefreshCw size={14} /> : <ShieldCheck size={14} />}
          <span>{proof.running ? "Running guard proof" : proof.canRun ? proof.actionLabel : proof.disabledReason}</span>
        </button>
      </section>
    </ContractPanel>
  );
}

export function MutationApplyExecutionPanel({ execution, title = "Deliberate Apply Execution" }: { execution: MutationApplyExecutionView; title?: string }) {
  const applied = execution.status === "Apply completed";
  const executeDisabled = applied || !execution.canExecute || execution.running || !execution.onExecute;
  return (
    <ContractPanel title={title} tone={execution.tone} icon={Lock}>
      <section className={`mutation-apply-panel mutation-apply-panel-${execution.tone}`}>
        <div className="mutation-apply-head">
          <span>OPS-5B-B apply armature</span>
          <strong>{execution.status}</strong>
          <p>{execution.summary}</p>
        </div>
        <div className="mutation-requirement-grid" aria-label={`${title} requirements`}>
          {execution.requirements.map((requirement) => (
            <article className={`mutation-requirement mutation-requirement-${requirement.state}`} key={requirement.label}>
              <span>{requirement.label}</span>
              <strong>{requirement.value}</strong>
              {requirement.detail ? <em>{requirement.detail}</em> : null}
            </article>
          ))}
        </div>
        {execution.operatorControls !== "hidden" ? (
          <div className="mutation-apply-operator">
            <label>
              <span>Type confirmation</span>
              <input
                autoCapitalize="off"
                autoComplete="off"
                onChange={(event) => execution.onConfirmationChange(event.target.value)}
                placeholder={execution.confirmationPhrase}
                spellCheck={false}
                type="text"
                value={execution.confirmationValue}
              />
            </label>
            <label className="mutation-apply-consent">
              <input checked={execution.rollbackConsent} onChange={(event) => execution.onRollbackConsentChange(event.target.checked)} type="checkbox" />
              <span>Rollback consent for this exact preview</span>
            </label>
          </div>
        ) : null}
        {execution.blockedReasons.length ? (
          <div className="mutation-guard-blockers">
            <span>Execution blockers</span>
            <strong>{execution.blockedReasons.join(", ")}</strong>
          </div>
        ) : null}
        <DefinitionRows rows={execution.rows} />
        <button className="mutation-apply-execute" disabled={executeDisabled} onClick={execution.onExecute} type="button">
          {applied ? <CheckCircle2 size={14} /> : <Lock size={14} />}
          <span>{execution.running ? "Executing apply" : applied ? "Applied - run new preview" : execution.canExecute ? execution.actionLabel : execution.disabledReason}</span>
        </button>
      </section>
    </ContractPanel>
  );
}

export function ReviewApprovalPanel({
  approved,
  disabled = false,
  disabledReason,
  mutationCopy,
  onApprove,
  primaryLabel = "Approve preview review",
  rows,
  summary,
  title,
}: {
  approved: boolean;
  disabled?: boolean;
  disabledReason?: string;
  mutationCopy: string;
  onApprove: () => void;
  primaryLabel?: string;
  rows: Array<[string, string]>;
  summary: string;
  title: string;
}) {
  return (
    <section className={approved ? "review-approval-panel review-approval-panel-approved" : "review-approval-panel"} aria-label={title}>
      <div className="review-approval-head">
        <ShieldCheck size={18} />
        <div>
          <span>{approved ? "Review approved" : "Human review required"}</span>
          <strong>{title}</strong>
          <p>{summary}</p>
        </div>
      </div>
      <DefinitionRows rows={rows} />
      <div className="review-approval-footer">
        <div>
          <Lock size={14} />
          <span>{mutationCopy}</span>
        </div>
        <button disabled={disabled || approved} onClick={onApprove} type="button">
          <ShieldCheck size={14} />
          <span>{approved ? "Approved" : disabled ? disabledReason || "Preview required" : primaryLabel}</span>
        </button>
      </div>
    </section>
  );
}

export function ReviewDecisionLedger({
  items,
  routeDetail,
  routeLabel,
  summary,
  title,
  tone = "muted",
}: {
  items: ReviewDecisionLedgerItem[];
  routeDetail: string;
  routeLabel: string;
  summary: string;
  title: string;
  tone?: ReviewDecisionLedgerItem["tone"];
}) {
  return (
    <section className={`review-decision-ledger review-decision-ledger-${tone}`} aria-label={title}>
      <header>
        <div>
          <span>Decision ledger</span>
          <strong>{title}</strong>
          <p>{summary}</p>
        </div>
        <em>{routeLabel}</em>
      </header>
      <div className="review-decision-ledger-grid">
        {items.map((item) => (
          <article className={`review-decision-ledger-item review-decision-ledger-item-${item.tone}`} key={item.label}>
            {item.tone === "blocked" ? <XCircle size={13} /> : item.tone === "ready" || item.tone === "selected" ? <CheckCircle2 size={13} /> : <CircleDashed size={13} />}
            <div>
              <span>{item.label}</span>
              <strong>{item.value}</strong>
              <em>{item.detail}</em>
            </div>
          </article>
        ))}
      </div>
      <footer>
        <Lock size={14} />
        <span>{routeDetail}</span>
      </footer>
    </section>
  );
}

export function ReviewBatchDeltaMap({
  emptyLabel = "Preview required",
  items,
  onSelectItem,
  stats,
  subtitle,
  title,
}: {
  emptyLabel?: string;
  items: ReviewBatchDeltaItem[];
  onSelectItem?: (id: string) => void;
  stats: ReviewBatchDeltaStat[];
  subtitle: string;
  title: string;
}) {
  const lanes = [
    { id: "backend" as const, label: "Backend preview", tones: new Set<ReviewBatchDeltaTone>(["backend", "review", "muted"]) },
    { id: "included" as const, label: "Included after review", tones: new Set<ReviewBatchDeltaTone>(["included", "moving"]) },
    { id: "excluded" as const, label: "Excluded / skipped", tones: new Set<ReviewBatchDeltaTone>(["excluded"]) },
  ];

  return (
    <section className={`review-batch-delta-map${items.length ? "" : " empty"}`} aria-label={title}>
      <header>
        <div>
          <span>Batch delta</span>
          <strong>{title}</strong>
          <p>{subtitle}</p>
        </div>
        <div className="review-batch-delta-stats">
          {stats.map((stat) => (
            <article className={`review-batch-delta-stat review-batch-delta-stat-${stat.tone}`} key={stat.label}>
              <span>{stat.label}</span>
              <strong>{stat.value}</strong>
              <em>{stat.detail}</em>
            </article>
          ))}
        </div>
      </header>
      <div className="review-batch-delta-lanes">
        {lanes.map((lane) => {
          const laneItems = items.filter((item) => lane.tones.has(item.tone));
          return (
            <section className={`review-batch-delta-lane review-batch-delta-lane-${lane.id}`} key={lane.id}>
              <div>
                <span>{lane.label}</span>
                <strong>{laneItems.length}</strong>
              </div>
              <div className="review-batch-delta-items">
                {laneItems.length ? (
                  laneItems.slice(0, 12).map((item) => {
                    const className = `review-batch-delta-item review-batch-delta-item-${item.tone}${item.focused ? " focused" : ""}`;
                    const content = (
                      <>
                        <strong>{compactText(item.label, 42)}</strong>
                        <em>{compactText(item.value, 48)}</em>
                      </>
                    );
                    return onSelectItem ? (
                      <button className={className} key={item.id} onClick={() => onSelectItem(item.id)} title={item.detail} type="button">
                        {content}
                      </button>
                    ) : (
                      <span className={className} key={item.id} title={item.detail}>
                        {content}
                      </span>
                    );
                  })
                ) : (
                  <span className="review-batch-delta-empty">{items.length ? "none" : emptyLabel}</span>
                )}
              </div>
            </section>
          );
        })}
      </div>
    </section>
  );
}

export function ReviewItemImpactPanel({
  action,
  steps,
  subtitle,
  title,
  tone = "muted",
}: {
  action?: ReactNode;
  steps: ReviewItemImpactStep[];
  subtitle: string;
  title: string;
  tone?: ReviewItemImpactStep["tone"];
}) {
  return (
    <section className={`review-item-impact review-item-impact-${tone}`} aria-label={title}>
      <header>
        <span>Item impact</span>
        <strong>{title}</strong>
        <p>{subtitle}</p>
      </header>
      <div className="review-item-impact-flow">
        {steps.map((step, index) => (
          <div className="review-item-impact-flow-node" key={step.label}>
            <article className={`review-item-impact-step review-item-impact-step-${step.tone}`}>
              {step.tone === "blocked" ? <XCircle size={13} /> : step.tone === "ready" || step.tone === "selected" ? <CheckCircle2 size={13} /> : <CircleDashed size={13} />}
              <div>
                <span>{step.label}</span>
                <strong>{step.value}</strong>
                <em>{step.detail}</em>
              </div>
            </article>
            {index < steps.length - 1 ? <ArrowRight className="review-item-impact-arrow" size={14} /> : null}
          </div>
        ))}
      </div>
      {action ? <div className="review-item-impact-action">{action}</div> : null}
    </section>
  );
}

export function BrainFirstOperationLayout({
  brain,
  command,
  commandRef,
  decision,
  evidence,
  footer,
  inspector,
  mode,
  proofTabs,
  steps,
}: {
  brain: ReactNode;
  command: ReactNode;
  commandRef?: Ref<HTMLDivElement>;
  decision?: ReactNode;
  evidence?: ReactNode;
  footer?: ReactNode;
  inspector?: ReactNode;
  mode: "health" | "grow" | "maintenance" | "matrix" | "chat";
  proofTabs?: GuidedProofTab[];
  steps?: GuidedOperationStep[];
}) {
  return (
    <section className={`brain-first-operation brain-first-operation-${mode}${steps?.length ? " brain-first-operation-guided" : ""}`}>
      {steps?.length ? <GuidedOperationStepper steps={steps} /> : null}
      <div className="brain-first-primary">
        <div className="brain-first-brain-stage">{brain}</div>
        <aside className={inspector ? "brain-first-command-surface guided-operation-side" : "brain-first-command-surface"} ref={commandRef}>
          {decision ? <div className="brain-first-decision-slot">{decision}</div> : null}
          <div className="guided-operation-command-slot">{command}</div>
          {inspector ? <div className="guided-operation-inspector-slot">{inspector}</div> : null}
        </aside>
      </div>
      {proofTabs?.length ? <GuidedProofTabs tabs={proofTabs} /> : evidence}
      {footer ? <div className="brain-first-footer">{footer}</div> : null}
    </section>
  );
}

export function OperationReviewFrame({
  brain,
  context,
  decision,
  decisionRef,
  footer,
  mode,
  proofTabs,
  steps,
}: {
  brain: ReactNode;
  context?: ReactNode;
  decision: ReactNode;
  decisionRef?: Ref<HTMLDivElement>;
  footer?: ReactNode;
  mode: "health" | "grow" | "maintenance" | "matrix" | "chat";
  proofTabs?: GuidedProofTab[];
  steps?: GuidedOperationStep[];
}) {
  return (
    <section className={`operation-review-frame operation-review-frame-${mode}${steps?.length ? " operation-review-frame-guided" : ""}`}>
      <div className="operation-review-workspace">
        {steps?.length ? <OperatorRunway steps={steps} /> : null}
        <div className="operation-review-grid">
          <main className="operation-review-decision" ref={decisionRef}>
            {decision}
          </main>
          <aside className="operation-review-context" aria-label="Operation brain context">
            <div className="operation-review-brain">{brain}</div>
            {context ? <div className="operation-review-inspector">{context}</div> : null}
          </aside>
        </div>
      </div>
      {proofTabs?.length ? <GuidedProofTabs tabs={proofTabs} /> : null}
      {footer ? <div className="brain-first-footer">{footer}</div> : null}
    </section>
  );
}

function OperatorRunway({ steps }: { steps: GuidedOperationStep[] }) {
  return (
    <aside className="operation-review-runway" aria-label="Operator runway">
      <div className="operation-review-runway-head">
        <span>Operator runway</span>
        <strong>Step-by-step control</strong>
      </div>
      <nav className="ops-runway-stepper" aria-label="Operation workflow">
        {steps.map((step, index) => (
          <article className={`ops-runway-step ops-runway-step-${step.state}`} key={`${step.label}:${index}`}>
            <span>{index + 1}</span>
            <div>
              <strong>{step.label}</strong>
              {step.detail ? <em>{step.detail}</em> : null}
            </div>
          </article>
        ))}
      </nav>
    </aside>
  );
}

function GuidedOperationStepper({ steps }: { steps: GuidedOperationStep[] }) {
  return (
    <nav className="guided-operation-stepper" aria-label="Guided operation steps">
      {steps.map((step, index) => (
        <article className={`guided-step guided-step-${step.state}`} key={`${step.label}:${index}`}>
          <span>{index + 1}</span>
          <div>
            <strong>{step.label}</strong>
            {step.detail ? <em>{step.detail}</em> : null}
          </div>
        </article>
      ))}
    </nav>
  );
}

function GuidedProofTabs({ tabs }: { tabs: GuidedProofTab[] }) {
  if (!tabs.length) return null;
  return (
    <details className="guided-proof-drawer guided-proof-stack" aria-label="Operation technical details">
      <summary className="guided-proof-stack-head">
        <span>Technical proof</span>
        <strong>Backend contract, guards and raw evidence stay available here without driving the workflow.</strong>
      </summary>
      <div className="guided-proof-accordions">
        {tabs.map((tab) => (
          <details className={`guided-proof-disclosure guided-proof-disclosure-${tab.tone || "mock"}`} key={tab.id}>
            <summary>
              <span>{tab.label}</span>
              <em>{tab.tone === "ready" ? "loaded" : tab.tone === "blocked" ? "guarded" : "waiting"}</em>
            </summary>
            <div className={`guided-proof-panel guided-proof-panel-${tab.tone || "mock"}`}>{tab.content}</div>
          </details>
        ))}
      </div>
    </details>
  );
}

export function ContractPanel({ children, icon: Icon, title, tone }: { children: ReactNode; icon: LucideIcon; title: string; tone: "ready" | "mock" | "blocked" }) {
  return (
    <article className={`mode-panel mode-panel-${tone}`}>
      <Icon size={17} />
      <div>
        <h2>{title}</h2>
        {children}
      </div>
    </article>
  );
}

export function EvidenceList({ empty, items }: { empty?: string; items: Array<{ label: string; value: string }> }) {
  if (!items.length) return <p className="mode-empty-copy">{empty || "No evidence available."}</p>;
  return (
    <ul className="evidence-list">
      {items.map((item) => (
        <li key={`${item.label}:${item.value}`}>
          <strong>{item.label}</strong>
          <span>{item.value}</span>
        </li>
      ))}
    </ul>
  );
}

export function NodeEvidenceList({ empty, nodes }: { empty: string; nodes: BrainRenderNode[] }) {
  return (
    <EvidenceList
      empty={empty}
      items={nodes.map((node) => ({
        label: compactId(node.id),
        value: compactNodeEvidence(node),
      }))}
    />
  );
}

export function DefinitionRows({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="definition-rows">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function MiniBrainContext({ context }: { context: OpsWorkspaceContext }) {
  return (
    <ContractPanel title="Brain Context" tone={context.graphNodeCount ? "ready" : "mock"} icon={Database}>
      <div className="mini-brain-context">
        <div>
          <strong>{context.graphNodeCount.toLocaleString()}</strong>
          <span>shown nodes</span>
        </div>
        <div>
          <strong>{context.routeSegments.length}</strong>
          <span>route segments</span>
        </div>
        <div>
          <strong>{context.documentAnchorCount}</strong>
          <span>anchors</span>
        </div>
      </div>
    </ContractPanel>
  );
}

export function EmptyBlock({ body, title }: { body: string; title: string }) {
  return (
    <div className="mode-empty-block">
      <CircleDashed size={18} />
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

export function ReadinessTile({ title, value, tone }: { title: string; value: string; tone: "ready" | "mock" | "blocked" }) {
  return (
    <article className={`readiness-tile readiness-tile-${tone}`}>
      <span>{title}</span>
      <strong>{value}</strong>
    </article>
  );
}
