import { useEffect, useMemo, useState } from "react";
import { ArrowRight } from "lucide-react";

import {
  correctAfterQuery,
  planCorrectionAfterQuery,
  type ContextCorrectionMode,
  type ContextCorrectionPlanResponse,
  type ContextCorrectionResponse,
} from "../api/contextCorrectionClient";
import type { BrainSceneModel } from "../brain/brainSceneModel";
import {
  buildContextRunInsight,
  type ContextConflictEvidenceSide,
  type ContextInsightSeverity,
  type ContextRunInsight,
  type ContextSuggestedActionCard,
} from "../mission/contextRunInsight";
import type { MissionProjection } from "../mission/missionProjection";
import type { MissionComposerViewModel } from "./MissionComposer";

type ContextLivePanelProps = {
  activeBrainId?: string | null;
  composer: MissionComposerViewModel;
  focusedNodeIds?: string[];
  mission: MissionProjection | null;
  model: BrainSceneModel;
  onCorrectionApplied?: (response: ContextCorrectionResponse) => void;
  onClearFocus?: () => void;
  onFocusNodeIds?: (nodeIds: string[]) => void;
  onOpenResults: () => void;
};

type ContextStageState = "done" | "running" | "waiting" | "blocked";

type ContextStage = {
  detail: string;
  id: string;
  label: string;
  state: ContextStageState;
};

type ContextRailTab = "used" | "conflicts" | "correct";

type ContextCorrectionApplyState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "success"; response: ContextCorrectionResponse }
  | { kind: "error"; message: string };

type ContextCorrectionPlanState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "ready"; plan: ContextCorrectionPlanResponse }
  | { kind: "error"; message: string };

type ContextRailDisplayCard = {
  detail: string;
  evidence?: ContextConflictEvidenceSide[];
  id: string;
  kicker: string;
  nodeIds: string[];
  reason?: string;
  severity: ContextInsightSeverity;
  title: string;
};

export function ContextLivePanel({
  activeBrainId,
  composer,
  focusedNodeIds = [],
  mission,
  model,
  onCorrectionApplied,
  onClearFocus,
  onFocusNodeIds,
  onOpenResults,
}: ContextLivePanelProps) {
  const [activeTab, setActiveTab] = useState<ContextRailTab>("used");
  const [selectedActionId, setSelectedActionId] = useState<string | null>(null);
  const [correctionMode, setCorrectionMode] = useState<ContextCorrectionMode>("revise");
  const [correctionText, setCorrectionText] = useState("");
  const [planState, setPlanState] = useState<ContextCorrectionPlanState>({ kind: "idle" });
  const [applyState, setApplyState] = useState<ContextCorrectionApplyState>({ kind: "idle" });
  const running = Boolean(composer.running || mission?.status === "running");
  const stages = contextStages(mission, composer, model);
  const progress = contextProgress(stages, mission, running);
  const preview = contextPreview(mission, running, composer.liveEventSummary);
  const insight = buildContextRunInsight(mission, { error: composer.lastError, running });
  const tone = composer.lastError ? "failed" : running ? "running" : mission ? "ready" : "idle";
  const reviewCount = insight.conflicts.length + insight.openQuestions.length + insight.qualitySignals.filter((signal) => signal.severity !== "info").length;
  const rawRailCounts = useMemo(
    () => ({
      conflicts: reviewCount,
      correct: insight.suggestedActions.length,
      used: insight.usedMemories.length + insight.sources.length,
    }),
    [insight.sources.length, insight.suggestedActions.length, insight.usedMemories.length, reviewCount],
  );
  const [railCounts, setRailCounts] = useState(rawRailCounts);
  const selectedAction = useMemo(() => {
    if (!insight.suggestedActions.length) return null;
    const explicit = insight.suggestedActions.find((action) => action.id === selectedActionId);
    return explicit || insight.suggestedActions[0];
  }, [insight.suggestedActions, selectedActionId]);

  useEffect(() => {
    setRailCounts(rawRailCounts);
  }, [mission?.id]);

  useEffect(() => {
    setRailCounts((current) => {
      if (!mission) return rawRailCounts;
      return {
        conflicts: Math.max(current.conflicts, rawRailCounts.conflicts),
        correct: Math.max(current.correct, rawRailCounts.correct),
        used: Math.max(current.used, rawRailCounts.used),
      };
    });
  }, [mission, rawRailCounts.conflicts, rawRailCounts.correct, rawRailCounts.used]);

  useEffect(() => {
    if (!selectedActionId && insight.suggestedActions.length) {
      setSelectedActionId(insight.suggestedActions[0].id);
    }
    if (selectedActionId && !insight.suggestedActions.some((action) => action.id === selectedActionId)) {
      setSelectedActionId(insight.suggestedActions[0]?.id || null);
    }
  }, [insight.suggestedActions, selectedActionId]);

  useEffect(() => {
    setApplyState({ kind: "idle" });
    setPlanState({ kind: "idle" });
    setCorrectionText("");
  }, [mission?.id]);

  useEffect(() => {
    setPlanState({ kind: "idle" });
    setApplyState({ kind: "idle" });
  }, [correctionText, selectedAction?.id]);

  const previewCorrectionPlan = async () => {
    const text = correctionText.trim();
    if (!mission || !selectedAction || !text || planState.kind === "running") return;
    setPlanState({ kind: "running" });
    try {
      const plan = await planCorrectionAfterQuery({
        brainId: activeBrainId || mission.requestPlan.body.brain_id,
        correctionPrompt: text,
        queryText: insight.query || mission.query || mission.requestPlan.body.query_text || "AGVM context correction",
        returnedAnswer: correctionReturnedAnswer(mission, selectedAction),
        searchId: mission.id,
        selectedAction: {
          action: selectedAction.action,
          detail: selectedAction.detail,
          evidence: selectedAction.evidence,
          guarded: selectedAction.guarded,
          id: selectedAction.id,
          reason: selectedAction.reason,
          title: selectedAction.title,
        },
        targetNodeIds: selectedAction.nodeIds,
        usedEvidenceNodeIds: insight.usedMemories.flatMap((memory) => memory.nodeIds),
      });
      setCorrectionMode(plan.correction_mode);
      setPlanState({ kind: "ready", plan });
    } catch (error) {
      setPlanState({ kind: "error", message: error instanceof Error ? error.message : "Correction planning failed" });
    }
  };

  const applyCorrection = async () => {
    if (!mission || !selectedAction || planState.kind !== "ready" || applyState.kind === "running") return;
    const plan = planState.plan;
    setApplyState({ kind: "running" });
    try {
      const response = await correctAfterQuery({
        brainId: activeBrainId || mission.requestPlan.body.brain_id,
        correctionMode: plan.correction_mode || correctionMode,
        correctionText: plan.correction_text,
        queryText: insight.query || mission.query || mission.requestPlan.body.query_text || "AGVM context correction",
        returnedAnswer: correctionReturnedAnswer(mission, selectedAction),
        searchId: mission.id,
        targetNodeIds: plan.target_node_ids?.length ? plan.target_node_ids : selectedAction.nodeIds,
        usedEvidenceNodeIds: plan.used_evidence_node_ids?.length ? plan.used_evidence_node_ids : insight.usedMemories.flatMap((memory) => memory.nodeIds),
      });
      setApplyState({ kind: "success", response });
      onCorrectionApplied?.(response);
    } catch (error) {
      setApplyState({ kind: "error", message: error instanceof Error ? error.message : "Correction apply failed" });
    }
  };

  return (
    <aside className={`context-run-panel context-intelligence-rail ${tone}`} aria-label="Live context status">
      <header>
        <i />
        <div>
          <span>Search status</span>
          <strong>{contextPanelTitle(running, mission, composer.lastError)}</strong>
        </div>
      </header>

      <div className="context-stage-progress" aria-label={`Context progress ${progress}%`}>
        <span style={{ width: `${progress}%` }} />
      </div>

      <nav className="context-rail-tabs" aria-label="Context insight sections">
        {(["used", "conflicts", "correct"] as const).map((tab) => (
          <button className={activeTab === tab ? "active" : ""} data-tab={tab} key={tab} onClick={() => setActiveTab(tab)} type="button">
            <em>{railTabStep(tab)}</em>
            <span>{railTabLabel(tab)}</span>
            <strong>{railCounts[tab]}</strong>
          </button>
        ))}
      </nav>

      <div className="context-rail-body">
        <ContextRailTabPanel
          activeTab={activeTab}
          applyState={applyState}
          correctionMode={correctionMode}
          correctionText={correctionText}
          focusedNodeIds={focusedNodeIds}
          insight={insight}
          onApplyCorrection={applyCorrection}
          onClearFocus={onClearFocus}
          onCorrectionTextChange={setCorrectionText}
          onFocusNodeIds={onFocusNodeIds}
          onPreviewCorrectionPlan={previewCorrectionPlan}
          onSelectCorrectionAction={setSelectedActionId}
          planState={planState}
          selectedActionId={selectedAction?.id || null}
        />
      </div>

      <section className="context-mini-package" aria-label="Context package preview">
        <span>{preview.kicker}</span>
        <strong>{preview.title}</strong>
        <p>{preview.body}</p>
      </section>

      <button className="context-result-button" disabled={!mission} onClick={onOpenResults} type="button">
        <span>{mission ? "Read Details" : "Details unavailable"}</span>
        <ArrowRight size={14} />
      </button>
    </aside>
  );
}

function ContextRailTabPanel({
  activeTab,
  applyState,
  correctionMode,
  correctionText,
  focusedNodeIds,
  insight,
  onApplyCorrection,
  onClearFocus,
  onCorrectionTextChange,
  onFocusNodeIds,
  onPreviewCorrectionPlan,
  onSelectCorrectionAction,
  planState,
  selectedActionId,
}: {
  activeTab: ContextRailTab;
  applyState: ContextCorrectionApplyState;
  correctionMode: ContextCorrectionMode;
  correctionText: string;
  focusedNodeIds: string[];
  insight: ContextRunInsight;
  onApplyCorrection: () => void;
  onClearFocus?: () => void;
  onCorrectionTextChange: (text: string) => void;
  onFocusNodeIds?: (nodeIds: string[]) => void;
  onPreviewCorrectionPlan: () => void;
  onSelectCorrectionAction: (id: string) => void;
  planState: ContextCorrectionPlanState;
  selectedActionId: string | null;
}) {
  const tabIntro = railTabIntro(activeTab);
  if (activeTab === "used") {
    const usedCards = [
      ...insight.usedMemories.map((memory) => ({
        detail: memory.reason || memory.detail,
        id: `memory-${memory.id}`,
        kicker: memory.state === "candidate" ? "Candidate memory" : "Used memory",
        nodeIds: memory.nodeIds,
        severity: "info" as ContextInsightSeverity,
        title: memory.title,
      })),
      ...insight.sources.map((source) => ({
        detail: source.why || source.detail,
        id: `source-${source.id}`,
        kicker: source.kind === "document" ? "Document source" : source.kind === "trace" ? "Source trace" : "Source",
        nodeIds: source.nodeIds,
        severity: "info" as ContextInsightSeverity,
        title: source.title,
      })),
    ];
    return (
      <section className="context-rail-tab-panel context-rail-tab-panel-used">
        <ContextRailIntro intro={tabIntro} />
        <ContextRailCardList
          cards={usedCards}
          emptyBody="When AGVM retrieves, every memory and source used for the answer appears here with clickable brain focus."
          emptyTitle="No evidence yet"
          focusedNodeIds={focusedNodeIds}
          onClearFocus={onClearFocus}
          onFocusNodeIds={onFocusNodeIds}
        />
      </section>
    );
  }

  if (activeTab === "conflicts") {
    const conflictCards = [
      ...insight.conflicts.map((conflict) => ({
        detail: conflict.detail,
        evidence: conflict.evidence,
        id: `conflict-${conflict.id}`,
        kicker: conflict.kind === "metacognition" ? "Memory signal" : "Conflict",
        nodeIds: conflict.nodeIds,
        reason: conflict.reason,
        severity: conflict.severity,
        title: conflict.title,
      })),
      ...insight.openQuestions.map((question) => ({
        detail: question.detail,
        id: `question-${question.id}`,
        kicker: "Question",
        nodeIds: question.nodeIds,
        severity: "watch" as ContextInsightSeverity,
        title: question.question,
      })),
      ...insight.qualitySignals
        .filter((signal) => signal.severity !== "info")
        .map((signal) => ({
          detail: signal.detail,
          id: `quality-${signal.id}`,
          kicker: "Quality signal",
          nodeIds: signal.nodeIds,
          severity: signal.severity,
          title: signal.title,
        })),
    ];
    return (
      <section className="context-rail-tab-panel context-rail-tab-panel-conflicts">
        <ContextRailIntro intro={tabIntro} />
        <ContextRailCardList
          cards={conflictCards}
          emptyBody="If two memories disagree, or the query leaves an unresolved slot, AGVM stages it here before the client trusts the context."
          emptyTitle="No conflicts visible"
          focusedNodeIds={focusedNodeIds}
          onClearFocus={onClearFocus}
          onFocusNodeIds={onFocusNodeIds}
        />
      </section>
    );
  }

  return (
    <section className="context-rail-tab-panel context-rail-tab-panel-correct">
      <ContextRailIntro intro={tabIntro} />
      <ContextCorrectionFlow
        actions={insight.suggestedActions}
        applyState={applyState}
        correctionMode={correctionMode}
        correctionText={correctionText}
        focusedNodeIds={focusedNodeIds}
        onApplyCorrection={onApplyCorrection}
        onClearFocus={onClearFocus}
        onCorrectionTextChange={onCorrectionTextChange}
        onFocusNodeIds={onFocusNodeIds}
        onPreviewCorrectionPlan={onPreviewCorrectionPlan}
        onSelectAction={onSelectCorrectionAction}
        planState={planState}
        selectedActionId={selectedActionId}
      />
    </section>
  );
}

function ContextCorrectionFlow({
  actions,
  applyState,
  correctionMode,
  correctionText,
  focusedNodeIds,
  onApplyCorrection,
  onClearFocus,
  onCorrectionTextChange,
  onFocusNodeIds,
  onPreviewCorrectionPlan,
  onSelectAction,
  planState,
  selectedActionId,
}: {
  actions: ContextSuggestedActionCard[];
  applyState: ContextCorrectionApplyState;
  correctionMode: ContextCorrectionMode;
  correctionText: string;
  focusedNodeIds: string[];
  onApplyCorrection: () => void;
  onClearFocus?: () => void;
  onCorrectionTextChange: (text: string) => void;
  onFocusNodeIds?: (nodeIds: string[]) => void;
  onPreviewCorrectionPlan: () => void;
  onSelectAction: (id: string) => void;
  planState: ContextCorrectionPlanState;
  selectedActionId: string | null;
}) {
  const selectedAction = actions.find((action) => action.id === selectedActionId) || actions[0] || null;
  const selectedCard = selectedAction
    ? {
        detail: selectedAction.detail,
        evidence: selectedAction.evidence,
        id: `selected-${selectedAction.id}`,
        kicker: selectedAction.guarded ? "Selected correction path" : "Selected suggestion",
        nodeIds: selectedAction.nodeIds,
        reason: selectedAction.reason,
        severity: selectedAction.action === "repair" ? ("warning" as ContextInsightSeverity) : ("watch" as ContextInsightSeverity),
        title: selectedAction.title,
      }
    : null;
  const canPreview = Boolean(selectedAction && correctionText.trim() && planState.kind !== "running" && applyState.kind !== "running");
  const canApply = Boolean(selectedAction && planState.kind === "ready" && applyState.kind !== "running");
  return (
    <div className="context-correct-flow">
      {!actions.length ? (
        <article className="context-rail-empty">
          <span>Nothing to apply</span>
          <strong>No correction staged</strong>
          <p>When a retrieval exposes a conflict, weak evidence or unresolved memory slot, the guarded correction flow appears here.</p>
        </article>
      ) : (
        <>
          <div className="context-correct-actions" aria-label="Suggested correction paths">
            {actions.slice(0, 8).map((action) => {
              const active = action.id === selectedAction?.id;
              return (
                <button
                  className={`context-correct-action ${active ? "active" : ""}`}
                  key={action.id}
                  onClick={() => {
                    onSelectAction(action.id);
                    if (action.nodeIds.length) onFocusNodeIds?.(action.nodeIds);
                  }}
                  type="button"
                >
                  <span>{actionTitlePrefix(action)}</span>
                  <strong>{compactText(action.title, 64)}</strong>
                  <em>{action.nodeIds.length ? `${action.nodeIds.length} linked` : "text-only"}</em>
                </button>
              );
            })}
          </div>

          {selectedCard ? <ContextRailCard card={selectedCard} focusedNodeIds={focusedNodeIds} onClearFocus={onClearFocus} onFocusNodeIds={onFocusNodeIds} /> : null}

          <label className="context-correct-field">
            <span>Correction guidance</span>
            <textarea
              onChange={(event) => onCorrectionTextChange(event.target.value)}
              placeholder="Explain what is wrong, what should be kept, or what the brain should ask/repair. AGVM will turn this into a guarded ingest plan."
              rows={4}
              value={correctionText}
            />
          </label>

          <div className="context-correct-controls">
            <button className="context-correct-preview" disabled={!canPreview} onClick={onPreviewCorrectionPlan} type="button">
              {planState.kind === "running" ? "Planning..." : "Preview repair plan"}
            </button>
            <button className="context-correct-apply" disabled={!canApply} onClick={onApplyCorrection} type="button">
              {applyState.kind === "running" ? "Applying..." : "Apply repair"}
            </button>
          </div>

          <ContextCorrectionPlanCard correctionMode={correctionMode} planState={planState} />
          <ContextCorrectionReceipt applyState={applyState} />
        </>
      )}
    </div>
  );
}

function ContextCorrectionPlanCard({ correctionMode, planState }: { correctionMode: ContextCorrectionMode; planState: ContextCorrectionPlanState }) {
  if (planState.kind === "idle") {
    return (
      <article className="context-correct-plan context-correct-plan-idle">
        <span>Repair planner</span>
        <strong>Write guidance, then preview the plan</strong>
        <p>AGVM will infer whether to revise, supersede, replace, archive or delete, then prepare source material for guarded ingest.</p>
      </article>
    );
  }
  if (planState.kind === "running") {
    return (
      <article className="context-correct-plan context-correct-plan-running">
        <span>Repair planner</span>
        <strong>Reading correction intent</strong>
        <p>AGVM is comparing your guidance with the selected memory path and active metamemory policy.</p>
      </article>
    );
  }
  if (planState.kind === "error") {
    return (
      <article className="context-correct-plan context-correct-plan-error">
        <span>Repair plan blocked</span>
        <strong>Could not prepare the correction plan</strong>
        <p>{compactText(planState.message, 190)}</p>
      </article>
    );
  }
  const plan = planState.plan;
  return (
    <article className={`context-correct-plan context-correct-plan-ready mode-${plan.correction_mode || correctionMode}`}>
      <span>{plan.source === "llm" ? "AGVM repair plan" : "Safe fallback repair plan"}</span>
      <strong>{compactText(plan.human_summary || correctionModeLabel(plan.correction_mode), 130)}</strong>
      <dl>
        <div>
          <dt>Action</dt>
          <dd>{correctionModeLabel(plan.correction_mode)}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd>{Math.round((plan.confidence || 0) * 100)}%</dd>
        </div>
      </dl>
      <p>{compactText(plan.rationale, 220)}</p>
      <details>
        <summary>Source AGVM will ingest</summary>
        <pre>{plan.correction_text}</pre>
      </details>
      {plan.warnings.length ? <em>{plan.warnings.slice(0, 2).join(" / ")}</em> : null}
    </article>
  );
}

function ContextCorrectionReceipt({ applyState }: { applyState: ContextCorrectionApplyState }) {
  if (applyState.kind === "idle") {
    return (
      <article className="context-correct-receipt context-correct-receipt-idle">
        <span>Guarded apply</span>
        <p>No memory changes happen until you press Apply correction. The graph refreshes after a successful correction.</p>
      </article>
    );
  }
  if (applyState.kind === "running") {
    return (
      <article className="context-correct-receipt context-correct-receipt-running">
        <span>Applying correction</span>
        <p>AGVM is writing the correction and refreshing the memory graph.</p>
      </article>
    );
  }
  if (applyState.kind === "error") {
    return (
      <article className="context-correct-receipt context-correct-receipt-error">
        <span>Apply blocked</span>
        <p>{compactText(applyState.message, 190)}</p>
      </article>
    );
  }
  const summary = applyState.response.action_summary || {};
  const persisted = Array.isArray(summary.persisted_node_ids) ? summary.persisted_node_ids.length : 0;
  const updated = Array.isArray(summary.updated_node_ids) ? summary.updated_node_ids.length : 0;
  const removed = Array.isArray(summary.removed_node_ids) ? summary.removed_node_ids.length : 0;
  return (
    <article className="context-correct-receipt context-correct-receipt-success">
      <span>Correction applied</span>
      <strong>{compactText(applyState.response.correction_id, 42)}</strong>
      <p>
        {persisted} new node{persisted === 1 ? "" : "s"}, {updated} updated, {removed} removed.
      </p>
    </article>
  );
}

function ContextRailIntro({ intro }: { intro: ReturnType<typeof railTabIntro> }) {
  return (
    <article className={`context-rail-intro context-rail-intro-${intro.tone}`}>
      <span>{intro.kicker}</span>
      <strong>{intro.title}</strong>
      <p>{intro.body}</p>
    </article>
  );
}

function ContextRailCardList({
  cards,
  emptyBody,
  emptyTitle,
  focusedNodeIds,
  onClearFocus,
  onFocusNodeIds,
}: {
  cards: ContextRailDisplayCard[];
  emptyBody: string;
  emptyTitle: string;
  focusedNodeIds: string[];
  onClearFocus?: () => void;
  onFocusNodeIds?: (nodeIds: string[]) => void;
}) {
  if (!cards.length) {
    return (
      <article className="context-rail-empty">
        <span>Nothing to show</span>
        <strong>{emptyTitle}</strong>
        <p>{emptyBody}</p>
      </article>
    );
  }

  return (
    <div className="context-rail-card-list">
      {cards.slice(0, 12).map((card) => (
        <ContextRailCard card={card} focusedNodeIds={focusedNodeIds} key={card.id} onClearFocus={onClearFocus} onFocusNodeIds={onFocusNodeIds} />
      ))}
    </div>
  );
}

function ContextRailCard({
  card,
  focusedNodeIds,
  onClearFocus,
  onFocusNodeIds,
}: {
  card: ContextRailDisplayCard;
  focusedNodeIds: string[];
  onClearFocus?: () => void;
  onFocusNodeIds?: (nodeIds: string[]) => void;
}) {
  const hasNodeFocus = card.nodeIds.length > 0;
  const active = hasNodeFocus && card.nodeIds.some((nodeId) => focusedNodeIds.includes(nodeId));
  const detailDiffersFromReason = !card.reason || card.reason !== card.detail;
  const focus = () => {
    if (!hasNodeFocus) return;
    onFocusNodeIds?.(card.nodeIds);
  };

  return (
    <button
      className={`context-rail-card context-rail-card-${card.severity} ${active ? "active" : ""}`}
      onClick={focus}
      onFocus={focus}
      onMouseEnter={focus}
      type="button"
    >
      <span>{card.kicker}</span>
      <strong>{compactText(card.title, 84)}</strong>
      {detailDiffersFromReason ? <p>{compactText(card.detail, 148)}</p> : null}
      {card.reason ? (
        <small className="context-rail-card-reason">
          <b>Why</b>
          {compactText(card.reason, 170)}
        </small>
      ) : null}
      {card.evidence?.length ? (
        <ul className="context-rail-card-evidence" aria-label="Conflict evidence">
          {card.evidence.slice(0, 2).map((side) => (
            <li key={`${side.label}-${side.nodeId || side.text}`}>
              <b>{side.label}</b>
              <span>{compactText(side.text, 130)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {hasNodeFocus ? <em>{card.nodeIds.length} linked node{card.nodeIds.length === 1 ? "" : "s"}</em> : <em>Text-only signal</em>}
    </button>
  );
}

function railTabLabel(tab: ContextRailTab) {
  if (tab === "used") return "Evidence";
  if (tab === "conflicts") return "Review";
  return "Correct";
}

function actionTitlePrefix(action: ContextSuggestedActionCard) {
  if (action.action === "clarify") return "Clarify";
  if (action.action === "open_source") return "Source";
  if (action.action === "repair") return "Repair";
  if (action.action === "review") return "Review";
  return "Action";
}

function correctionModeLabel(mode: ContextCorrectionMode) {
  if (mode === "supersede") return "Supersede selected memory";
  if (mode === "replace") return "Replace selected memory";
  if (mode === "archive") return "Archive selected memory";
  if (mode === "delete") return "Delete selected memory";
  return "Revise and keep history";
}

function correctionReturnedAnswer(mission: MissionProjection, action: ContextSuggestedActionCard) {
  const packageText = mission.payloadMarkdown.trim();
  if (packageText) return compactText(packageText, 2400);
  const summary = [
    mission.summary.payloadState,
    mission.summary.clientCanProceed,
    mission.summary.semanticAi,
    mission.summary.spatialAi,
    action.title,
    action.detail,
  ]
    .map((value) => String(value || "").trim())
    .filter(Boolean)
    .join("\n");
  return summary || "AGVM context package exposed a correction action.";
}

function railTabStep(tab: ContextRailTab) {
  if (tab === "used") return "1";
  if (tab === "conflicts") return "2";
  return "3";
}

function railTabIntro(tab: ContextRailTab) {
  if (tab === "used") {
    return {
      body: "AGVM is not a black box: the answer can be traced back to memories, documents and the brain areas that were activated.",
      kicker: "Transparency",
      title: "See what the brain relied on",
      tone: "used",
    };
  }
  if (tab === "conflicts") {
    return {
      body: "Contradictions and weak evidence are surfaced before they silently become confident answers.",
      kicker: "Governance",
      title: "Find disagreement before the client does",
      tone: "conflicts",
    };
  }
  return {
    body: "AGVM can suggest a repair path, but changes remain reversible and guarded until explicitly approved.",
    kicker: "Control",
    title: "Preview the correction before applying it",
    tone: "correct",
  };
}

function contextStages(mission: MissionProjection | null, composer: MissionComposerViewModel, model: BrainSceneModel): ContextStage[] {
  const running = Boolean(composer.running || mission?.status === "running");
  const semantic = mission?.summary.semanticAi || "";
  const hasSemantic = Boolean(mission && semantic && !/not started|waiting|none/i.test(semantic));
  const landings = mission ? realLandingCount(mission) : 0;
  const visualTargets = mission ? renderableVisualTargetCount(mission, model) : 0;
  const contextNodes = mission?.payloadNodeIds.length || 0;
  const documents = mission?.documentRefs.length || 0;
  const terminal = mission?.live?.terminalForClient === true;
  const failed = Boolean(composer.lastError);

  return [
    {
      id: "request",
      label: "Request",
      detail: mission ? "captured" : composer.queryText.trim() ? "ready to run" : "draft",
      state: mission ? "done" : composer.queryText.trim() ? "running" : "waiting",
    },
    {
      id: "contract",
      label: "Intent",
      detail: hasSemantic ? semantic : running ? "understanding" : "waiting",
      state: hasSemantic ? "done" : running ? "running" : "waiting",
    },
    {
      id: "landing",
      label: "Landing",
      detail: landings ? `${landings} landing${landings === 1 ? "" : "s"} / ${visualTargets} visible` : running ? "locating" : "waiting",
      state: landings ? "done" : running ? "running" : "waiting",
    },
    {
      id: "context",
      label: "Memory context",
      detail: contextNodes ? `${contextNodes} memor${contextNodes === 1 ? "y" : "ies"}` : running ? "collecting" : "not started",
      state: contextNodes ? "done" : running ? "running" : "waiting",
    },
    {
      id: "documents",
      label: "Documents",
      detail: documents ? `${documents} ref${documents === 1 ? "" : "s"}` : running ? "watching refs" : "none",
      state: documents ? "done" : running ? "running" : "waiting",
    },
    {
      id: "delivery",
      label: "Ready state",
      detail: failed ? "failed" : terminal ? "ready" : running ? "live" : mission ? clientStateLabel(mission) : "waiting",
      state: failed ? "blocked" : terminal ? "done" : running ? "running" : mission ? "waiting" : "waiting",
    },
  ];
}

function contextProgress(stages: ContextStage[], mission: MissionProjection | null, running: boolean) {
  if (mission?.live?.terminalForClient === true) return 100;
  const raw = stages.reduce((total, stage) => total + (stage.state === "done" ? 1 : stage.state === "running" ? 0.48 : 0), 0);
  const percent = Math.round((raw / Math.max(1, stages.length)) * 100);
  if (running) return Math.min(88, Math.max(14, percent));
  if (mission) return Math.min(96, Math.max(62, percent));
  return 0;
}

function contextPanelTitle(running: boolean, mission: MissionProjection | null, error: string | null) {
  if (error) return "Context request failed";
  if (running) return "Reading the brain";
  if (mission?.live?.terminalForClient) return "Context ready";
  if (mission) return mission.summary.clientCanProceed || "Context available";
  return "Awaiting mission";
}

function contextPreview(mission: MissionProjection | null, running: boolean, liveSummary: string) {
  if (!mission) {
    return {
      kicker: "Context preview",
      title: "No package yet",
      body: "Ask for the context the agent needs. Landings, memory and source references will appear here while the search runs.",
    };
  }
  if (running) {
    return {
      kicker: "Live package",
      title: mission.summary.payloadState || "streaming",
      body: liveSummary || "AGVM is still collecting memory. Details becomes the full reader when the context is ready.",
    };
  }
  const lines = mission.payloadMarkdown
    .split(/\r?\n/)
    .map((line) => line.replace(/^[-#*\s]+/, "").trim())
    .filter((line) => line.length > 24 && !/^task|requested|delivery|source|missing|unresolved/i.test(line));
  return {
    kicker: mission.live?.terminalForClient ? "Terminal package" : "Latest package",
    title: mission.summary.payloadState || clientStateLabel(mission),
    body: compactText(lines[0] || mission.summary.clientCanProceed || "Open Details for the readable context, source references and path trace.", 210),
  };
}

function compactText(value: string, maxLength: number) {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trim()}...`;
}

function clientStateLabel(mission: MissionProjection) {
  if (mission.live?.terminalForClient === true) return "ready";
  if (mission.live?.terminalForClient === false) return "not terminal";
  if (mission.status === "running") return "streaming";
  return mission.summary.clientCanProceed || mission.status;
}

function landingStateLabel(mission: MissionProjection) {
  const landings = realLandingCount(mission);
  if (landings > 0) return `${landings} certified`;
  if (mission.candidateNodeIds.length > 0) return `0 / ${mission.candidateNodeIds.length} candidates`;
  return "waiting";
}

function routeTruthLabel(mission: MissionProjection) {
  const pathCount = mission.projection.rawPathCount || 0;
  const pathSuffix = pathCount > 0 ? ` / ${pathCount} path${pathCount === 1 ? "" : "s"}` : "";
  if (mission.routeSegments.length > 0) return `${mission.routeSegments.length} edge${mission.routeSegments.length === 1 ? "" : "s"}${pathSuffix}`;
  if (mission.routeNodeIds.length > 1) return `${mission.routeNodeIds.length} nodes${pathSuffix}`;
  if (mission.completePaths) return "requested";
  const summary = String(mission.summary.pathTruth || "").trim();
  if (!summary || summary === "0" || summary.toLowerCase() === "none") return "not materialized";
  return summary;
}

function realLandingCount(mission: MissionProjection | null) {
  if (!mission) return 0;
  const anchorLandings = mission.projection.anchors.filter((anchor) => anchor.role === "landing").length;
  return Math.max(mission.landingNodeIds.length, anchorLandings);
}

function renderableVisualTargetCount(mission: MissionProjection, model: BrainSceneModel) {
  const graphNodeIds = new Set(model.graphNodes.map((node) => node.id));
  const ids = new Set<string>();
  for (const id of mission.landingNodeIds) if (graphNodeIds.has(id)) ids.add(id);
  for (const id of mission.payloadNodeIds) if (graphNodeIds.has(id)) ids.add(id);
  for (const id of mission.routeNodeIds) if (graphNodeIds.has(id)) ids.add(id);
  for (const id of mission.documentNodeIds) if (graphNodeIds.has(id)) ids.add(id);
  for (const id of mission.candidateNodeIds) if (graphNodeIds.has(id)) ids.add(id);
  for (const anchor of mission.projection.anchors) {
    if (isRenderableContextAnchor(anchor)) ids.add(anchor.id);
  }
  return ids.size;
}

function isRenderableContextAnchor(anchor: MissionProjection["projection"]["anchors"][number]) {
  const packageRole = String(anchor.packageRole || "").toLowerCase();
  if (packageRole !== "debug_only") return true;
  const source = String(anchor.source || "").toLowerCase();
  return anchor.role === "landing" && source.includes("search_map_2d_truth");
}

function compactContextId(id: string) {
  return id.length > 18 ? `${id.slice(0, 8)}...${id.slice(-6)}` : id;
}
