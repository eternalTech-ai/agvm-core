import { ArrowRight, Play, Search } from "lucide-react";

import type { MissionMode, MissionRefsPolicy, MissionRequestPlan, MissionTool } from "../mission/missionProjection";
import { Dropdown, type DropdownOption } from "../components/Dropdown";
import { SegmentedControl, type SegmentedControlOption } from "../components/SegmentedControl";

export type MissionComposerViewModel = {
  queryText: string;
  tool: MissionTool;
  mode: MissionMode;
  refsPolicy: MissionRefsPolicy;
  completePaths: boolean;
  includeAnswerDemo: boolean;
  requestPlan: MissionRequestPlan;
  loading: boolean;
  running: boolean;
  executionMode: "live" | "fixture";
  lastError: string | null;
  liveEventCount: number;
  liveEventLabel: string;
  liveEventSummary: string;
  activeMission: {
    clientState: string;
    documents: string;
    id: string;
    payloadState: string;
    phase: string;
  } | null;
};

export type MissionComposerProps = {
  view: MissionComposerViewModel;
  onQueryTextChange: (value: string) => void;
  onToolChange: (value: MissionTool) => void;
  onModeChange: (value: MissionMode) => void;
  onRefsPolicyChange: (value: MissionRefsPolicy) => void;
  onCompletePathsChange: (value: boolean) => void;
  onIncludeAnswerDemoChange: (value: boolean) => void;
  onOpenResults: () => void;
  onRun: () => void;
};

type MissionCommandBarProps = Pick<MissionComposerProps, "onQueryTextChange" | "onRun" | "view">;

type MissionSettingsPanelProps = Pick<
  MissionComposerProps,
  "onCompletePathsChange" | "onIncludeAnswerDemoChange" | "onModeChange" | "onRefsPolicyChange" | "onToolChange" | "view"
>;

export function MissionComposer({
  view,
  onQueryTextChange,
  onToolChange,
  onModeChange,
  onRefsPolicyChange,
  onCompletePathsChange,
  onIncludeAnswerDemoChange,
  onOpenResults,
  onRun,
}: MissionComposerProps) {
  const queryText = view.queryText || "";
  const canRun = Boolean(queryText.trim()) && !view.loading && !view.running;
  const contractLabel = missionContractLabel(view);

  return (
    <form
      className="mission-composer context-command-composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (canRun) onRun();
      }}
    >
      <div className="mission-composer-head">
        <div>
          <span>Ask memory</span>
          <strong title={`${view.requestPlan.tool} -> ${view.requestPlan.endpoint}`}>{contractLabel}</strong>
        </div>
        <em>{missionRuntimeLabel(view)}</em>
      </div>
      <div className="mission-command-row">
        <label className="mission-input">
          <Search size={16} />
          <input
            onChange={(event) => onQueryTextChange(event.target.value)}
            placeholder="Ask what context the agent should recover from this brain"
            type="text"
            value={queryText}
          />
        </label>
        <button className={view.running ? "run active" : "run"} disabled={!canRun} type="submit">
          <Play size={15} />
          <span>{runButtonLabel(view, canRun)}</span>
        </button>
      </div>
      <div className="mission-controls">
        <SegmentedControl className="mission-control-group mission-strategy-group" density="compact" label="Strategy" onChange={onModeChange} options={missionModeOptions()} value={view.mode} />

        <SegmentedControl className="mission-control-group mission-lane-group" density="compact" label="Tool" onChange={onToolChange} options={missionToolOptions()} value={view.tool} />

        <Dropdown className="mission-select-group" label="Document policy" onChange={onRefsPolicyChange} options={missionRefsPolicyOptions()} value={view.refsPolicy} />

        <fieldset className="mission-control-group mission-runtime-group">
          <legend>Advanced</legend>
          <div className="mission-toggle-pair">
            <ContextOptionToggle
              active={view.completePaths}
              description="wait for path trace"
              label="path trace"
              onChange={onCompletePathsChange}
              title="Wait for route/path trace before the context package is considered complete."
            />
            <ContextOptionToggle
              active={view.includeAnswerDemo}
              description="context remains separate"
              label="answer preview"
              onChange={onIncludeAnswerDemoChange}
              title="Also ask AGVM to produce a secondary answer preview from the approved context package. This is not the retrieval contract."
            />
          </div>
        </fieldset>
      </div>
      <MissionContextReceipt view={view} onOpenResults={onOpenResults} />
      {view.lastError ? <div className="mission-live-error">Context request failed: {view.lastError}</div> : null}
    </form>
  );
}

export function MissionCommandBar({ view, onQueryTextChange, onRun }: MissionCommandBarProps) {
  const queryText = view.queryText || "";
  const canRun = Boolean(queryText.trim()) && !view.loading && !view.running;

  return (
    <form
      className="mission-composer context-command-composer context-cockpit-command-bar"
      onSubmit={(event) => {
        event.preventDefault();
        if (canRun) onRun();
      }}
    >
      <div className="mission-command-row">
        <label className="mission-input">
          <Search size={16} />
          <input
            aria-label="Context query"
            onChange={(event) => onQueryTextChange(event.target.value)}
            placeholder="Ask the brain for context..."
            type="text"
            value={queryText}
          />
        </label>
        <button className={view.running ? "run active" : "run"} disabled={!canRun} type="submit">
          <Play size={15} />
          <span>{runButtonLabel(view, canRun)}</span>
        </button>
      </div>
      <div className={`context-command-status ${view.running ? "live" : view.activeMission ? "ready" : "idle"}`} aria-live="polite">
        <i />
        <span>{missionRuntimeLabel(view)}</span>
      </div>
    </form>
  );
}

export function MissionSettingsPanel({
  view,
  onToolChange,
  onModeChange,
  onRefsPolicyChange,
}: MissionSettingsPanelProps) {
  return (
    <section className="context-search-settings context-search-settings-compact" aria-label="Search settings">
      <div className="context-search-settings-head">
        <span>Settings</span>
      </div>
      <CompactSettingGroup label="Depth" onChange={onModeChange} options={missionModeOptions()} value={view.mode} />
      <CompactSettingGroup label="Lane" onChange={onToolChange} options={missionToolOptions()} value={view.tool} />
      <CompactSettingGroup label="Sources" onChange={onRefsPolicyChange} options={missionRefsPolicyOptions()} value={view.refsPolicy} />
    </section>
  );
}

function CompactSettingGroup<TValue extends string>({
  label,
  onChange,
  options,
  value,
}: {
  label: string;
  onChange: (value: TValue) => void;
  options: Array<DropdownOption<TValue> | SegmentedControlOption<TValue>>;
  value: TValue;
}) {
  return (
    <div className="context-compact-setting-group">
      <span>{label}</span>
      <div className="context-compact-setting-options">
        {options.map((option) => (
          <button className={option.value === value ? "active" : ""} key={option.value} onClick={() => onChange(option.value)} title={option.meta || option.label} type="button">
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function MissionContextReceipt({ view, onOpenResults }: { view: MissionComposerViewModel; onOpenResults: () => void }) {
  const mission = view.activeMission;
  const tone = view.lastError ? "failed" : view.running ? "running" : mission ? "ready" : "idle";
  const title = view.lastError
    ? "Context request failed"
    : view.running
      ? view.liveEventLabel || "Reading memory"
      : mission
        ? mission.payloadState || mission.clientState
        : "No context package yet";
  const subtitle = view.lastError
    ? view.lastError
    : view.running
      ? view.liveEventSummary || `${view.liveEventCount} live update${view.liveEventCount === 1 ? "" : "s"} observed`
      : mission
        ? mission.id
        : "Run a context command to create a readable package.";

  return (
    <section className={`mission-context-receipt ${tone}`} aria-label="Context command receipt">
      <i aria-hidden="true" />
      <div>
        <span>{view.running ? "Live search" : mission ? "Latest context" : "Context state"}</span>
        <strong title={title}>{title}</strong>
        <small title={subtitle}>{subtitle}</small>
      </div>
      <dl>
        <div>
          <dt>Client</dt>
          <dd>{mission?.clientState || (view.running ? "streaming" : "waiting")}</dd>
        </div>
        <div>
          <dt>Memory</dt>
          <dd>{mission?.payloadState || (view.running ? "materializing" : "none")}</dd>
        </div>
        <div>
          <dt>Docs</dt>
          <dd>{mission?.documents || "no refs"}</dd>
        </div>
        <div>
          <dt>Phase</dt>
          <dd>{mission?.phase || missionRuntimeLabel(view)}</dd>
        </div>
      </dl>
      <button disabled={!mission && !view.running} onClick={onOpenResults} type="button">
        <span>View Results</span>
        <ArrowRight size={13} />
      </button>
    </section>
  );
}

function missionRefsPolicyOptions(): DropdownOption<MissionRefsPolicy>[] {
  return [
    { value: "refs_only", label: "Refs only", meta: "return references" },
    { value: "top_raw", label: "Top raw", meta: "include strongest source" },
    { value: "all_raw", label: "All raw", meta: "include all source text" },
  ];
}

function missionModeOptions(): SegmentedControlOption<MissionMode>[] {
  return [
    { value: "flash", label: "Flash", meta: "fast scan", title: "Fastest context pass with lower inspection depth." },
    { value: "balanced", label: "Balanced", meta: "default", title: "Default context retrieval balance." },
    { value: "heavy", label: "Heavy", meta: "deeper", title: "Deeper retrieval pass with more backend work." },
    { value: "forensic", label: "Forensic", meta: "audit", title: "Most exact inspection mode for source-heavy retrieval." },
  ];
}

function missionToolOptions(): SegmentedControlOption<MissionTool>[] {
  return [
    { value: "retrieve_context", label: "Context", meta: "memory path", title: "Natural AGVM context retrieval." },
    { value: "retrieve_document_workspace", label: "Docs", meta: "source refs", title: "Document workspace retrieval with exact source refs." },
  ];
}

function ContextOptionToggle({
  active,
  description,
  label,
  onChange,
  title,
}: {
  active: boolean;
  description: string;
  label: string;
  onChange: (value: boolean) => void;
  title: string;
}) {
  return (
    <label className={`mission-toggle ${active ? "active" : ""}`} title={title}>
      <input checked={active} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
      <span>{label}</span>
      <em className="mission-flag-explainer">{description}</em>
    </label>
  );
}

function missionContractLabel(view: MissionComposerViewModel) {
  const lane = view.tool === "retrieve_document_workspace" ? "Documents" : "Context";
  const refs = view.refsPolicy === "refs_only" ? "refs" : view.refsPolicy === "top_raw" ? "top source" : "all sources";
  return `${lane} lane / ${view.mode} / ${refs}`;
}

function missionRuntimeLabel(view: MissionComposerViewModel) {
  if (view.running) return "Reading memory";
  if (view.lastError) return "Search failed";
  if (view.activeMission) return view.activeMission.phase;
  if (!(view.queryText || "").trim()) return "Ready for a question";
  if (view.executionMode === "fixture") return "Fixture mode";
  return "Ready";
}

function runButtonLabel(view: MissionComposerViewModel, canRun: boolean) {
  if (view.running) return "Running";
  if (!view.queryText.trim()) return "Type query";
  if (!canRun) return "Waiting";
  return view.executionMode === "fixture" ? "Preview fixture" : "Run context";
}
