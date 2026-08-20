import {
  ArrowRight,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clipboard,
  Cloud,
  Code2,
  Copy,
  Database,
  FileText,
  Info,
  KeyRound,
  Link,
  Lock,
  PlayCircle,
  Plug,
  Server,
  ShieldCheck,
  Terminal,
  Trash2,
  Upload,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { SegmentedControl, type SegmentedControlOption } from "../components/SegmentedControl";
import type { OpsWorkspaceContext } from "../ops/opsWorkspaceTypes";
import { ProductPageFrame } from "../shell/ProductPageFrame";

type ClientKind = "codex" | "claude" | "cursor" | "generic";
type RuntimeScope = "local" | "hosted";
type BrainPolicy = "fixed" | "ai_resolve_existing" | "ai_create_if_missing";
type PermissionProfile = "read_only_recall" | "agent_onboarding" | "preview_only_learning" | "full_local_operator";
type SetupStepId = "env" | "brain" | "safety" | "connect";
type CoachTopicId = SetupStepId | "overview" | "client" | "hosted" | "permissions" | "apply";
type SnippetKind = "client" | "env" | "config" | "prompt";
type TestState = "idle" | "running" | "ready" | "failed";
type BrainActionState = {
  brainId: string;
  detail: string;
  status: TestState;
  title: string;
};

type McpSetupDraft = {
  apiBaseUrl: string;
  brainId: string;
  brainDisplayName: string;
  brainPolicy: BrainPolicy;
  brainPurpose: string;
  clientKind: ClientKind;
  environmentId: string;
  organizationId: string;
  permissionProfile: PermissionProfile;
  runtimeScope: RuntimeScope;
  tenantId: string;
  userId: string;
};

type EnvSessionState = {
  error: string | null;
  importedAt: string | null;
  importedLabel: string | null;
  rawText: string;
  values: Record<string, string>;
};

type BackendEnvState = {
  configured: boolean;
  error: string | null;
  loading: boolean;
  masked: string;
  source: string;
};

type ClientConfigSafety = {
  allowedFamilies: string[];
  applyBlocked: boolean;
  applyEnabled: boolean;
  blockedFamilies: string[];
};

type SetupEnvStatusPayload = {
  detail?: unknown;
  provider?: {
    configured?: boolean;
    masked?: string;
    source?: string;
  };
};

type CheckResult = {
  detail: string;
  title: string;
} | null;

const storageKey = "agvm.mcpSetupDraft.v2";

const defaultEnvSession: EnvSessionState = {
  error: null,
  importedAt: null,
  importedLabel: null,
  rawText: "",
  values: {},
};

const defaultBackendEnvState: BackendEnvState = {
  configured: false,
  error: null,
  loading: false,
  masked: "",
  source: "unknown",
};

const defaultBrainActionState: BrainActionState = {
  brainId: "",
  detail: "",
  status: "idle",
  title: "",
};

const defaultDraft: McpSetupDraft = {
  apiBaseUrl: "http://127.0.0.1:8010",
  brainId: "",
  brainDisplayName: "Codex Project Memory",
  brainPolicy: "ai_create_if_missing",
  brainPurpose: "Persistent MCP memory for this AI client, independent from the dashboard active brain.",
  clientKind: "codex",
  environmentId: "local_self_hosted_dev",
  organizationId: "local_org",
  permissionProfile: "preview_only_learning",
  runtimeScope: "local",
  tenantId: "local_tenant",
  userId: "local_user",
};

const dockerMcpContainerName = "agvm_lab_api";
const localModuleVisibilityPolicy = "hide_unlicensed";
const generatedClientEnvBlocklist = new Set(["AGVM_MCP_CONFIG", "OPENAI_API_KEY"]);

const clientOptions: SegmentedControlOption<ClientKind>[] = [
  { label: "Codex", meta: "stdio", value: "codex" },
  { label: "Claude", meta: "desktop", value: "claude" },
  { label: "Cursor", meta: "mcp", value: "cursor" },
  { label: "Generic", meta: "json-rpc", value: "generic" },
];

const brainPolicyOptions: SegmentedControlOption<BrainPolicy>[] = [
  { label: "AI creates", meta: "new if missing", value: "ai_create_if_missing" },
  { label: "AI picks", meta: "existing only", value: "ai_resolve_existing" },
  { label: "Fixed", meta: "locked id", value: "fixed" },
];

const profileOptions: SegmentedControlOption<PermissionProfile>[] = [
  { label: "Recall", meta: "read only", value: "read_only_recall" },
  { label: "Onboard", meta: "registry", value: "agent_onboarding" },
  { label: "Preview", meta: "safe learn", value: "preview_only_learning" },
  { label: "Operator", meta: "apply gated", value: "full_local_operator" },
];

const setupSteps: Array<{
  done: string;
  goal: string;
  hint: string;
  id: SetupStepId;
  icon: LucideIcon;
  label: string;
  title: string;
}> = [
  {
    done: "Runtime fields are ready; provider key can be saved once in the AGVM API managed env.",
    goal: "Configure the local backend URL and the provider key. Cloud platform login/token is not active yet.",
    hint: "Backend key and local runtime",
    id: "env",
    icon: KeyRound,
    label: "Env keys",
    title: "Save provider key",
  },
  {
    done: "The bridge has an explicit local brain policy: fixed, AI resolves existing, or AI creates if missing.",
    goal: "Choose whether the MCP client is locked to one brain or must resolve its own brain before memory calls.",
    hint: "Sets local memory behavior",
    id: "brain",
    icon: Database,
    label: "Brain",
    title: "Pick the memory scope",
  },
  {
    done: "The generated config includes allowed and blocked permission families.",
    goal: "Choose whether the AI can only recall, create/resolve brains, preview learning, or apply writes after approval.",
    hint: "Explains tool access",
    id: "safety",
    icon: ShieldCheck,
    label: "Safety",
    title: "Set what the AI may do",
  },
  {
    done: "The bridge command, client config and first AI prompt are ready to copy.",
    goal: "Run the Docker API check, copy the app config, then let the AI app start the MCP bridge and send the first prompt.",
    hint: "Check, copy, prompt",
    id: "connect",
    icon: Terminal,
    label: "Connect",
    title: "Copy, launch, prompt",
  },
];

export function McpSetupWorkspace({ context }: { context: OpsWorkspaceContext }) {
  const [draft, setDraft] = useState<McpSetupDraft>(() => loadDraft(context.brainManagement.activeBrainId));
  const [envSession, setEnvSession] = useState<EnvSessionState>(defaultEnvSession);
  const [backendEnvState, setBackendEnvState] = useState<BackendEnvState>(defaultBackendEnvState);
  const [brainAction, setBrainAction] = useState<BrainActionState>(defaultBrainActionState);
  const [providerKey, setProviderKey] = useState("");
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [activeStep, setActiveStep] = useState<SetupStepId>("env");
  const [activeSnippet, setActiveSnippet] = useState<SnippetKind>("client");
  const [coachOpen, setCoachOpen] = useState(false);
  const [coachTopic, setCoachTopic] = useState<CoachTopicId>("overview");
  const [autoCoach, setAutoCoach] = useState(false);
  const [testState, setTestState] = useState<TestState>("idle");
  const [checkResult, setCheckResult] = useState<CheckResult>(null);

  const activeBrainId = context.brainManagement.activeBrainId;
  const effectiveBrainId = draft.brainId.trim() || activeBrainId || "simone_massaro";
  const effectiveApiBaseUrl = useMemo(() => (draft.apiBaseUrl.trim() || defaultDraft.apiBaseUrl).replace(/\/+$/, ""), [draft.apiBaseUrl]);
  const profileConfig = useMemo(() => permissionProfileConfig(draft.permissionProfile), [draft.permissionProfile]);
  const localConfig = useMemo(() => buildLocalConfig(draft, effectiveBrainId, profileConfig), [draft, effectiveBrainId, profileConfig]);
  const envBlock = useMemo(() => buildEnvBlock(draft, effectiveBrainId, profileConfig, providerKey), [draft, effectiveBrainId, profileConfig, providerKey]);
  const backendEnvBlock = useMemo(() => buildBackendEnvBlock(draft, effectiveBrainId, profileConfig, providerKey), [draft, effectiveBrainId, profileConfig, providerKey]);
  const clientConfig = useMemo(() => buildClientConfig(draft, envBlock), [draft, envBlock]);
  const operatorClientConfig = useMemo(() => {
    const operatorProfileConfig = permissionProfileConfig("full_local_operator");
    const operatorEnvBlock = buildEnvBlock(draft, effectiveBrainId, operatorProfileConfig, providerKey);
    return buildClientConfig({ ...draft, permissionProfile: "full_local_operator" }, operatorEnvBlock);
  }, [draft, effectiveBrainId, providerKey]);
  const clientConfigSafety = useMemo(() => inspectClientConfigSafety(clientConfig), [clientConfig]);
  const firstPrompt = useMemo(() => buildFirstPrompt(draft, effectiveBrainId), [draft, effectiveBrainId]);
  const snippets = useMemo(
    () => [
      { id: "client" as const, icon: Clipboard, label: "Client config", title: `${clientLabel(draft.clientKind)} MCP config`, text: clientConfig },
      { id: "env" as const, icon: Terminal, label: "Env", title: "Bridge environment", text: envBlock },
      { id: "config" as const, icon: Code2, label: "Advanced JSON", title: "Optional local MCP config JSON", text: JSON.stringify(localConfig, null, 2) },
      { id: "prompt" as const, icon: Brain, label: "Prompt", title: "First instruction for the AI", text: firstPrompt },
    ],
    [clientConfig, draft.clientKind, envBlock, firstPrompt, localConfig],
  );
  const activeSnippetData = snippets.find((snippet) => snippet.id === activeSnippet) || snippets[0];
  const activeIndex = setupSteps.findIndex((step) => step.id === activeStep);
  const activeMeta = setupSteps[activeIndex] || setupSteps[0];
  const canGoBack = activeIndex > 0;
  const hasNextStep = activeIndex < setupSteps.length - 1;
  const currentStepComplete = isStepComplete(activeStep, activeIndex, activeIndex, draft, envSession, effectiveBrainId, providerKey, testState);
  const canGoNext = hasNextStep && currentStepComplete;
  const nextStep = hasNextStep ? setupSteps[activeIndex + 1] : null;
  const primaryActionDisabled = hasNextStep ? !canGoNext : !currentStepComplete;

  useEffect(() => {
    void refreshBackendEnvStatus();
  }, [effectiveApiBaseUrl]);

  function updateDraft(patch: Partial<McpSetupDraft>) {
    setDraft((current) => ({ ...current, ...patch }));
    setBrainAction(defaultBrainActionState);
    setCopyError(null);
    setTestState("idle");
    setCheckResult(null);
  }

  function openCoach(topic: CoachTopicId) {
    setCoachTopic(topic);
    setCoachOpen(true);
  }

  function goToStep(stepId: SetupStepId) {
    setActiveStep(stepId);
    setActiveSnippet(snippetForStep(stepId));
    if (autoCoach) {
      setCoachTopic(stepId);
      setCoachOpen(true);
    }
  }

  function goBy(delta: number) {
    const next = setupSteps[Math.max(0, Math.min(setupSteps.length - 1, activeIndex + delta))];
    if (next) goToStep(next.id);
  }

  function runPrimaryStepAction() {
    if (hasNextStep) {
      goBy(1);
      return;
    }
    saveNonSecretDraft();
  }

  function saveNonSecretDraft() {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(storageKey, JSON.stringify(draft));
    }
    setSavedAt(new Date().toLocaleTimeString());
  }

  function clearDraft() {
    if (typeof window !== "undefined") window.localStorage.removeItem(storageKey);
    setDraft({ ...defaultDraft, brainId: "", brainDisplayName: defaultBrainDisplayName(activeBrainId) });
    setEnvSession(defaultEnvSession);
    setBackendEnvState(defaultBackendEnvState);
    setBrainAction(defaultBrainActionState);
    setProviderKey("");
    setSavedAt(null);
    setCopiedId(null);
    setCopyError(null);
    setTestState("idle");
    setCheckResult(null);
    goToStep("env");
  }

  async function refreshBackendEnvStatus() {
    if (typeof fetch === "undefined") return;
    setBackendEnvState((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await fetch(`${effectiveApiBaseUrl}/setup/env`, { headers: { Accept: "application/json" } });
      const payload = (await response.json()) as SetupEnvStatusPayload;
      if (!response.ok) throw new Error(readApiError(payload, response.status));
      const provider = payload.provider || {};
      setBackendEnvState({
        configured: Boolean(provider.configured),
        error: null,
        loading: false,
        masked: String(provider.masked || ""),
        source: String(provider.source || "missing"),
      });
    } catch (error) {
      setBackendEnvState((current) => ({
        ...current,
        error: error instanceof Error ? error.message : "Backend env status unavailable",
        loading: false,
      }));
    }
  }

  async function saveProviderKeyToBackend() {
    const key = providerKey.trim();
    if (!key) {
      setBackendEnvState((current) => ({ ...current, error: "Paste OPENAI_API_KEY before saving it to the backend." }));
      return;
    }
    if (typeof fetch === "undefined") return;
    setBackendEnvState((current) => ({ ...current, loading: true, error: null }));
    try {
      const body: Record<string, boolean | string> = {
        agvm_llm_enabled: true,
        openai_api_key: key,
      };
      if (draft.runtimeScope === "local" && draft.brainPolicy === "fixed") {
        body.agvm_default_brain_id = effectiveBrainId;
      }
      const response = await fetch(`${effectiveApiBaseUrl}/setup/env`, {
        body: JSON.stringify(body),
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        method: "POST",
      });
      const payload = (await response.json()) as SetupEnvStatusPayload;
      if (!response.ok) throw new Error(readApiError(payload, response.status));
      const provider = payload.provider || {};
      setBackendEnvState({
        configured: Boolean(provider.configured),
        error: null,
        loading: false,
        masked: String(provider.masked || maskSecret(key)),
        source: String(provider.source || "managed_runtime_env"),
      });
      setProviderKey("");
      setTestState("idle");
      setCheckResult(null);
    } catch (error) {
      setBackendEnvState((current) => ({
        ...current,
        error: error instanceof Error ? error.message : "Could not save server-side provider key.",
        loading: false,
      }));
    }
  }

  function updateEnvText(value: string) {
    setEnvSession((current) => ({ ...current, error: null, rawText: value }));
  }

  function updateProviderKey(value: string) {
    setProviderKey(value);
    setTestState("idle");
    setCheckResult(null);
  }

  function applyEnvText(text: string, label = "pasted .env") {
    const parsed = parseEnvText(text);
    const nextImportedAt = new Date().toLocaleTimeString();
    setEnvSession({
      error: parsed.error,
      importedAt: parsed.error ? null : nextImportedAt,
      importedLabel: label,
      rawText: parsed.error ? text : redactedEnvText(parsed.values),
      values: parsed.values,
    });
    if (parsed.error) return;
    const patch = draftPatchFromEnv(parsed.values);
    if (Object.keys(patch).length) {
      setDraft((current) => ({ ...current, ...patch }));
    }
    const secret = parsed.values.OPENAI_API_KEY || parsed.values.AGVM_HOSTED_ACCESS_TOKEN || "";
    if (secret) setProviderKey(secret);
    setTestState("idle");
    setCheckResult(null);
  }

  async function loadEnvFile(file: File | null | undefined) {
    if (!file) return;
    const text = await file.text();
    applyEnvText(text, file.name || ".env");
  }

  async function runLocalCheck() {
    setTestState("running");
    setCheckResult({ title: "Checking AGVM API", detail: "Calling /mcp/contracts from this browser." });
    if (typeof window === "undefined" || typeof fetch === "undefined") {
      setTestState("ready");
      setCheckResult({ title: "Runtime check skipped", detail: "Browser fetch is not available in this rendering context." });
      return;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 6500);
    try {
      const response = await fetch(`${effectiveApiBaseUrl}/mcp/contracts`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as { schema_version?: string; tools?: unknown[] };
      const toolCount = Array.isArray(payload.tools) ? payload.tools.length : 0;
      if (!payload.schema_version || toolCount < 1) throw new Error("MCP registry returned no tools");
      setTestState("ready");
      setCheckResult({
        title: "Connection verified",
        detail: `${toolCount} MCP tools visible from ${effectiveApiBaseUrl}. Start the stdio bridge next, then connect the AI client.`,
      });
    } catch (error) {
      setTestState("failed");
      setCheckResult({
        title: "Connection check failed",
        detail: `${error instanceof Error ? error.message : "Unknown error"}. Start the AGVM API on ${effectiveApiBaseUrl}, then run Check config again.`,
      });
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function runBrainEnsure() {
    if (typeof fetch === "undefined") return;
    if (draft.brainPolicy === "fixed") {
      setBrainAction({
        brainId: effectiveBrainId,
        detail: "Fixed mode uses this existing brain id in the generated MCP config. It does not create a new brain.",
        status: "ready",
        title: "Fixed brain selected",
      });
      return;
    }
    const displayName = draft.brainDisplayName.trim() || defaultDraft.brainDisplayName;
    if (!displayName) {
      setBrainAction({ brainId: "", detail: "Add a target display name before creating or verifying a brain.", status: "failed", title: "Brain name missing" });
      return;
    }
    setBrainAction({ brainId: "", detail: "Calling /mcp/brains/ensure with activation_policy=return_only.", status: "running", title: "Checking brain" });
    try {
      const response = await fetch(`${effectiveApiBaseUrl}/mcp/brains/ensure`, {
        body: JSON.stringify({
          activation_policy: "return_only",
          brain_id: draft.brainId.trim() || null,
          create_if_missing: draft.brainPolicy === "ai_create_if_missing",
          display_name: displayName,
          purpose: draft.brainPurpose.trim() || null,
        }),
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        method: "POST",
      });
      const payload = (await response.json()) as { brain_id?: string; created?: boolean; detail?: unknown; selected?: boolean; status?: string };
      if (!response.ok) throw new Error(readApiError(payload, response.status));
      const returnedBrainId = String(payload.brain_id || draft.brainId || "").trim();
      setBrainAction({
        brainId: returnedBrainId,
        detail: `${payload.created ? "Created" : "Verified"} ${returnedBrainId || displayName}. activation_policy=return_only, so the dashboard active brain was not changed.`,
        status: "ready",
        title: payload.created ? "Brain created" : "Brain verified",
      });
    } catch (error) {
      setBrainAction({
        brainId: "",
        detail: error instanceof Error ? error.message : "Could not verify or create the brain.",
        status: "failed",
        title: "Brain check failed",
      });
    }
  }

  async function copySnippet(id: string, value: string) {
    try {
      await writeClipboardText(value);
      setCopiedId(id);
      setCopyError(null);
      window.setTimeout(() => setCopiedId((current) => (current === id ? null : current)), 1400);
    } catch {
      setCopiedId(null);
      setCopyError("Clipboard permission was denied. The generated config is still visible: select the Client config block and copy it manually.");
    }
  }

  async function copyOperatorClientConfig() {
    setDraft((current) => ({ ...current, permissionProfile: "full_local_operator" }));
    setActiveStep("connect");
    setActiveSnippet("client");
    setTestState("idle");
    setCheckResult(null);
    await copySnippet("client", operatorClientConfig);
  }

  return (
    <ProductPageFrame
      actions={[]}
      className="mcp-setup-frame"
      eyebrow="MCP"
      icon={Plug}
      intent="A guided flow for connecting Codex, Claude, Cursor or any MCP client to an AGVM memory brain."
      metrics={[]}
      mode="mcp_setup"
      status={setupStatus(draft, providerKey, backendEnvState, testState)}
      title="AI Memory Setup"
    >
      <section className="mcp-setup-workspace mcp-setup-flow" data-testid="mcp-setup-workspace">
        <FlowHero backendEnvState={backendEnvState} draft={draft} envSession={envSession} effectiveBrainId={effectiveBrainId} onExplain={() => openCoach("overview")} providerKey={providerKey} testState={testState} />

        <FlowStepper activeStep={activeStep} backendEnvState={backendEnvState} draft={draft} envSession={envSession} effectiveBrainId={effectiveBrainId} onSelect={goToStep} providerKey={providerKey} testState={testState} />

        <section className="mcp-flow-workbench" aria-label="Guided MCP setup">
          <main className="mcp-flow-panel">
            <StepHeader activeIndex={activeIndex} meta={activeMeta} onExplain={() => openCoach(activeStep)} />
            <div className="mcp-step-actions">
              <button disabled={!canGoBack} onClick={() => goBy(-1)} type="button">
                <ChevronLeft size={15} />
                <span>Previous</span>
              </button>
              <button onClick={saveNonSecretDraft} type="button">
                <CheckCircle2 size={15} />
                <span>{savedAt ? `Saved ${savedAt}` : "Save setup"}</span>
              </button>
              <button className="mcp-step-primary-action" disabled={primaryActionDisabled} onClick={runPrimaryStepAction} type="button">
                <span>{nextStep ? `Next: ${nextStep.label}` : "Done"}</span>
                <ChevronRight size={15} />
              </button>
            </div>
            <StepBody
              activeStep={activeStep}
              backendEnvState={backendEnvState}
              backendEnvBlock={backendEnvBlock}
              brainAction={brainAction}
              checkResult={checkResult}
              clientConfig={clientConfig}
              clientConfigSafety={clientConfigSafety}
              copyError={copyError}
              copiedId={copiedId}
              draft={draft}
              envSession={envSession}
              effectiveBrainId={effectiveBrainId}
              onClear={clearDraft}
              onCopy={copySnippet}
              onDraftChange={updateDraft}
              onEnvFileLoad={loadEnvFile}
              onEnvLoad={applyEnvText}
              onEnvTextChange={updateEnvText}
              onExplainTopic={openCoach}
              onProviderKeyChange={updateProviderKey}
              onProviderKeySave={saveProviderKeyToBackend}
              onCopyOperatorClientConfig={copyOperatorClientConfig}
              onRunBrainEnsure={runBrainEnsure}
              onRunLocalCheck={runLocalCheck}
              profileConfig={profileConfig}
              providerKey={providerKey}
              testState={testState}
            />
          </main>

          <LiveOutputDrawer
            activeSnippet={activeSnippet}
            activeStep={activeStep}
            backendEnvState={backendEnvState}
            clientConfigSafety={clientConfigSafety}
            copyError={copyError}
            copied={copiedId === activeSnippetData.id}
            draft={draft}
            envSession={envSession}
            effectiveBrainId={effectiveBrainId}
            onCopy={() => void copySnippet(activeSnippetData.id, activeSnippetData.text)}
            onExplainTopic={openCoach}
            onSelect={setActiveSnippet}
            providerKey={providerKey}
            snippet={activeSnippetData}
            snippets={snippets}
            testState={testState}
          />
        </section>

        <AdvancedSetupDetails
          draft={draft}
          effectiveBrainId={effectiveBrainId}
          backendEnvState={backendEnvState}
          profileConfig={profileConfig}
          providerKey={providerKey}
        />
        {coachOpen ? (
          <CoachModal
            activeStep={activeStep}
            autoCoach={autoCoach}
            draft={draft}
            effectiveBrainId={effectiveBrainId}
            onClose={() => setCoachOpen(false)}
            onDisableAuto={() => {
              setAutoCoach(false);
              setCoachOpen(false);
            }}
            onGoToStep={goToStep}
            onTopic={openCoach}
            profileConfig={profileConfig}
            testState={testState}
            topic={coachTopic}
          />
        ) : null}
      </section>
    </ProductPageFrame>
  );
}

function FlowHero({
  backendEnvState,
  draft,
  envSession,
  effectiveBrainId,
  onExplain,
  providerKey,
  testState,
}: {
  backendEnvState: BackendEnvState;
  draft: McpSetupDraft;
  envSession: EnvSessionState;
  effectiveBrainId: string;
  onExplain: () => void;
  providerKey: string;
  testState: TestState;
}) {
  return (
    <section className="mcp-flow-hero" aria-label="MCP connection overview">
      <div className="mcp-flow-copy">
        <h2>Guided setup, one decision at a time.</h2>
        <p>Save the provider key first, then choose brain, safety and final connection output.</p>
        <button className="mcp-hero-help" onClick={onExplain} type="button">
          <Info size={14} />
          <span>Open guided walkthrough</span>
        </button>
      </div>
      <div className="mcp-connection-visual" aria-label="AI client to AGVM memory path">
        <ConnectionNode icon={Plug} label="AI Client" value={clientLabel(draft.clientKind)} />
        <span className="mcp-connection-line" aria-hidden="true" />
        <ConnectionNode icon={Server} label="AGVM Bridge" value="stdio MCP" />
        <span className="mcp-connection-line" aria-hidden="true" />
        <ConnectionNode icon={Database} label="Memory Scope" value={brainScopeLabel(draft, effectiveBrainId)} />
      </div>
      <div className="mcp-flow-status-strip">
        <SetupFact icon={KeyRound} label="Env" value={envStatusLabel(envSession, providerKey, backendEnvState)} />
        <SetupFact icon={ShieldCheck} label="Mode" value={profileLabel(draft.permissionProfile)} />
        <SetupFact icon={PlayCircle} label="Check" value={testLabel(testState)} />
      </div>
    </section>
  );
}

function ConnectionNode({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <article className="mcp-connection-node">
      <Icon size={17} />
      <span>{label}</span>
      <strong title={value}>{value}</strong>
    </article>
  );
}

function FlowStepper({
  activeStep,
  backendEnvState,
  draft,
  envSession,
  effectiveBrainId,
  onSelect,
  providerKey,
  testState,
}: {
  activeStep: SetupStepId;
  backendEnvState: BackendEnvState;
  draft: McpSetupDraft;
  envSession: EnvSessionState;
  effectiveBrainId: string;
  onSelect: (step: SetupStepId) => void;
  providerKey: string;
  testState: TestState;
}) {
  const activeIndex = setupSteps.findIndex((step) => step.id === activeStep);
  return (
    <nav className="mcp-flow-stepper" aria-label="MCP setup steps">
      {setupSteps.map((step, index) => {
        const Icon = step.icon;
        const active = step.id === activeStep;
        const complete = isStepComplete(step.id, index, activeIndex, draft, envSession, effectiveBrainId, providerKey, testState);
        return (
          <button
            aria-current={active ? "step" : undefined}
            className={`${active ? "active" : ""} ${complete ? "complete" : ""}`.trim()}
            data-step-id={step.id}
            key={step.id}
            onClick={() => onSelect(step.id)}
            type="button"
          >
            <span className="mcp-step-index">{complete ? <CheckCircle2 size={13} /> : index + 1}</span>
            <Icon size={15} />
            <strong>{step.label}</strong>
            <em>{stepPreview(step.id, draft, envSession, effectiveBrainId, providerKey, backendEnvState, testState)}</em>
            <small>{step.hint}</small>
          </button>
        );
      })}
    </nav>
  );
}

function StepHeader({ activeIndex, meta, onExplain }: { activeIndex: number; meta: (typeof setupSteps)[number]; onExplain: () => void }) {
  const Icon = meta.icon;
  return (
    <header className="mcp-flow-panel-head">
      <Icon size={18} />
      <div>
        <span>Step {activeIndex + 1} of {setupSteps.length}</span>
        <h3>{meta.title}</h3>
        <p>{meta.goal}</p>
      </div>
      <button className="mcp-panel-help" onClick={onExplain} type="button">
        <Info size={14} />
        <span>Explain</span>
      </button>
    </header>
  );
}

function StepBody({
  activeStep,
  backendEnvState,
  backendEnvBlock,
  brainAction,
  checkResult,
  clientConfig,
  clientConfigSafety,
  copyError,
  copiedId,
  draft,
  envSession,
  effectiveBrainId,
  onClear,
  onCopy,
  onDraftChange,
  onEnvFileLoad,
  onEnvLoad,
  onEnvTextChange,
  onExplainTopic,
  onProviderKeyChange,
  onProviderKeySave,
  onCopyOperatorClientConfig,
  onRunBrainEnsure,
  onRunLocalCheck,
  profileConfig,
  providerKey,
  testState,
}: {
  activeStep: SetupStepId;
  backendEnvState: BackendEnvState;
  backendEnvBlock: string;
  brainAction: BrainActionState;
  checkResult: CheckResult;
  clientConfig: string;
  clientConfigSafety: ClientConfigSafety;
  copyError: string | null;
  copiedId: string | null;
  draft: McpSetupDraft;
  envSession: EnvSessionState;
  effectiveBrainId: string;
  onClear: () => void;
  onCopy: (id: string, value: string) => Promise<void>;
  onDraftChange: (patch: Partial<McpSetupDraft>) => void;
  onEnvFileLoad: (file: File | null | undefined) => Promise<void>;
  onEnvLoad: (text: string, label?: string) => void;
  onEnvTextChange: (value: string) => void;
  onExplainTopic: (topic: CoachTopicId) => void;
  onProviderKeyChange: (value: string) => void;
  onProviderKeySave: () => Promise<void>;
  onCopyOperatorClientConfig: () => Promise<void>;
  onRunBrainEnsure: () => Promise<void>;
  onRunLocalCheck: () => Promise<void>;
  profileConfig: ReturnType<typeof permissionProfileConfig>;
  providerKey: string;
  testState: TestState;
}) {
  if (activeStep === "env") {
    return (
      <div className="mcp-step-body">
        <StepOutcome icon={KeyRound} title="Local keys and runtime" detail="Today this setup is local self-hosted. The only real token here is OPENAI_API_KEY for the AGVM backend; platform/cloud login is not active yet." />
        <div className="mcp-env-primary-grid">
          <label className="mcp-token-field">
            <span>OPENAI_API_KEY</span>
            <input
              autoComplete="off"
              onChange={(event) => onProviderKeyChange(event.target.value)}
              placeholder="Paste token only if needed"
              type="password"
              value={providerKey}
            />
            <div className="mcp-token-action-row">
              <button disabled={!providerKey.trim() || backendEnvState.loading} onClick={() => void onProviderKeySave()} type="button">
                <KeyRound size={13} />
                <strong>{backendEnvState.loading ? "Saving..." : "Save key to backend"}</strong>
              </button>
              <em>{backendProviderLabel(backendEnvState, providerKey)}</em>
            </div>
          </label>
          <label>
            <span>AGVM_API_BASE_URL</span>
            <input value={draft.apiBaseUrl} onChange={(event) => onDraftChange({ apiBaseUrl: event.target.value })} placeholder={defaultDraft.apiBaseUrl} />
            <em>Used by the MCP bridge when it calls AGVM.</em>
          </label>
          <div className="mcp-env-scope-note">
            <Cloud size={15} />
            <div>
              <strong>Cloud platform token: not available yet</strong>
              <span>Future paid cloud/Google login will live here. This local test uses the API URL and backend provider key only.</span>
            </div>
          </div>
        </div>
        <div className="mcp-setup-lane-grid" aria-label="Runtime mode explanation">
          <article className="active">
            <Server size={16} />
            <div>
              <strong>Local self-hosted</strong>
              <span>UI talks to your local AGVM API. The MCP bridge also calls this API.</span>
            </div>
          </article>
          <article>
            <Cloud size={16} />
            <div>
              <strong>AGVM Cloud later</strong>
              <span>No platform token is exposed today. Later this becomes login, vault, billing and module activation.</span>
            </div>
          </article>
        </div>
        <details className="mcp-env-advanced-panel" open={Boolean(envSession.rawText || envSession.error)}>
          <summary>
            <FileText size={14} />
            <span>Advanced import/export .env</span>
            <em>Optional: read a local .env file, paste raw text, or copy a manual backend template.</em>
          </summary>
          <div className="mcp-env-loader">
            <div className="mcp-env-actions">
              <label className="mcp-env-file-button mcp-env-action-card">
                <Upload size={14} />
                <span>
                  <strong>Load .env file</strong>
                  <small>Reads the file in this browser and fills these fields.</small>
                </span>
                <input
                  accept=".env,text/plain"
                  onChange={(event) => {
                    const file = event.currentTarget.files?.[0];
                    event.currentTarget.value = "";
                    void onEnvFileLoad(file);
                  }}
                  type="file"
                />
              </label>
              <button className="mcp-env-action-card" disabled={!envSession.rawText.trim()} onClick={() => onEnvLoad(envSession.rawText, "pasted .env")} type="button">
                <FileText size={14} />
                <span>
                  <strong>Use pasted .env</strong>
                  <small>Parses the text below and fills the fields.</small>
                </span>
              </button>
              <button className="mcp-env-action-card" onClick={() => void onCopy("backend-env", backendEnvBlock)} type="button">
                {copiedId === "backend-env" ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                <span>
                  <strong>{copiedId === "backend-env" ? "Copied" : "Copy manual .env"}</strong>
                  <small>Fallback template for editing server-side env manually.</small>
                </span>
              </button>
            </div>
            <details className="mcp-env-paste-panel" open={Boolean(envSession.rawText || envSession.error)}>
              <summary>
                <FileText size={14} />
                <span>Paste or inspect raw .env content</span>
              </summary>
              <label>
                <span>.env content</span>
                <textarea
                  onChange={(event) => onEnvTextChange(event.target.value)}
                  placeholder={[
                    "OPENAI_API_KEY=sk-...",
                    `AGVM_API_BASE_URL=${draft.apiBaseUrl || defaultDraft.apiBaseUrl}`,
                    `AGVM_MCP_BRAIN_POLICY=${draft.brainPolicy}`,
                  ].join("\n")}
                  value={envSession.rawText}
                />
              </label>
            </details>
            {envSession.error ? <div className="mcp-env-error">{envSession.error}</div> : null}
          </div>
        </details>
        {backendEnvState.error ? (
          <div className="mcp-env-error">
            <Info size={14} />
            <span>{backendEnvState.error}</span>
          </div>
        ) : null}
        <EnvChecklist backendEnvState={backendEnvState} envSession={envSession} providerKey={providerKey} draft={draft} effectiveBrainId={effectiveBrainId} />
      </div>
    );
  }

  if (activeStep === "brain") {
    const delegatedLocalBrain = draft.brainPolicy !== "fixed";
    const registryWriteAllowed = profileConfig.allowed_permission_families.includes("registry_write") && !profileConfig.blocked_permission_families.includes("registry_write");
    return (
      <div className="mcp-step-body">
        <StepOutcome icon={Database} title="Choose how the AI gets a brain" detail="This is only memory scope. It does not configure cloud, tokens or hosted tenants. The UI active brain can be different from the MCP brain." />
        <div className="mcp-context-help-row">
          <button onClick={() => onExplainTopic("brain")} type="button">
            <Info size={13} />
            <span>Explain brain policy</span>
          </button>
        </div>
        <div className="mcp-brain-policy-panel">
          <SegmentedControl
            density="normal"
            label="Brain policy"
            onChange={(brainPolicy) =>
              onDraftChange({
                brainPolicy,
                brainDisplayName: draft.brainDisplayName.trim() || humanizeBrainId(effectiveBrainId) || defaultDraft.brainDisplayName,
                runtimeScope: "local",
              })
            }
            options={brainPolicyOptions}
            value={draft.brainPolicy}
          />
          <div className="mcp-brain-policy-summary">
            <Database size={16} />
            <div>
              <strong>{brainPolicyTitle(draft.brainPolicy)}</strong>
              <span>{brainPolicyDescription(draft, effectiveBrainId)}</span>
            </div>
          </div>
          {draft.brainPolicy === "fixed" ? (
            <div className="mcp-flow-form-grid">
              <label>
                <span>Fixed brain id</span>
                <input value={draft.brainId} onChange={(event) => onDraftChange({ brainId: event.target.value, runtimeScope: "local" })} placeholder={effectiveBrainId} />
                <em>Use this when you already know the exact brain. The AI can call retrieve_context immediately.</em>
              </label>
            </div>
          ) : (
            <div className="mcp-flow-form-grid">
              <label>
                <span>Target display name</span>
                <input value={draft.brainDisplayName} onChange={(event) => onDraftChange({ brainDisplayName: event.target.value, runtimeScope: "local" })} placeholder={humanizeBrainId(effectiveBrainId) || defaultDraft.brainDisplayName} />
                <em>The AI uses this when it calls ensure_brain.</em>
              </label>
              <label>
                <span>Brain id hint</span>
                <input value={draft.brainId} onChange={(event) => onDraftChange({ brainId: event.target.value, runtimeScope: "local" })} placeholder="codex_project_memory" />
                <em>Optional stable id. This is not a fixed brain unless you choose Fixed.</em>
              </label>
              <label className="mcp-form-field-wide">
                <span>Purpose</span>
                <input value={draft.brainPurpose} onChange={(event) => onDraftChange({ brainPurpose: event.target.value, runtimeScope: "local" })} placeholder="What this AI should remember here" />
                <em>This becomes guidance for the AI-managed brain.</em>
              </label>
            </div>
          )}
          {delegatedLocalBrain && !registryWriteAllowed ? (
            <div className="mcp-env-error">
              <Lock size={14} />
              <span>Delegated brain policy needs registry_write. Switch Safety to Onboard, Preview or Operator before connecting.</span>
            </div>
          ) : null}
          <div className="mcp-brain-action-row">
            <button disabled={brainAction.status === "running" || (delegatedLocalBrain && !registryWriteAllowed)} onClick={() => void onRunBrainEnsure()} type="button">
              {brainAction.status === "running" ? <PlayCircle size={14} /> : <CheckCircle2 size={14} />}
              <span>{draft.brainPolicy === "fixed" ? "Verify fixed brain" : draft.brainPolicy === "ai_create_if_missing" ? "Create / verify now" : "Verify existing brain"}</span>
            </button>
            <div>
              <strong>{brainPolicyNextAction(draft.brainPolicy)}</strong>
              <span>{brainPolicyRuntimeNote(draft, effectiveBrainId)}</span>
            </div>
          </div>
          {brainAction.status !== "idle" ? (
            <div className={`mcp-check-result mcp-check-result-${brainAction.status}`}>
              {brainAction.status === "ready" ? <CheckCircle2 size={15} /> : brainAction.status === "failed" ? <Lock size={15} /> : <PlayCircle size={15} />}
              <div>
                <strong>{brainAction.title}</strong>
                <span>{brainAction.detail}</span>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    );
  }

  if (activeStep === "safety") {
    const delegatedLocalBrain = draft.brainPolicy !== "fixed";
    const registryWriteAllowed = profileConfig.allowed_permission_families.includes("registry_write") && !profileConfig.blocked_permission_families.includes("registry_write");
    const safetyCopy = safetyProfileCopy(draft.permissionProfile);
    return (
      <div className="mcp-step-body">
        <StepOutcome icon={ShieldCheck} title="Choose the AI's operating boundary" detail="This decides what MCP tools are visible to the AI before any backend call happens." />
        <div className="mcp-context-help-row">
          <button onClick={() => onExplainTopic("permissions")} type="button">
            <Info size={13} />
            <span>Explain families</span>
          </button>
          <button onClick={() => onExplainTopic("apply")} type="button">
            <Info size={13} />
            <span>Can the AI apply?</span>
          </button>
        </div>
        <SegmentedControl density="compact" label="Permission profile" onChange={(permissionProfile) => onDraftChange({ permissionProfile })} options={profileOptions} value={draft.permissionProfile} />
        <div className="mcp-safety-explainer">
          <ShieldCheck size={17} />
          <div>
            <strong>{safetyCopy.title}</strong>
            <span>{safetyCopy.body}</span>
          </div>
        </div>
        <div className="mcp-client-checklist">
          {safetyCopy.effects.map((effect, index) => (
            <article key={effect}>
              <strong>{index + 1}</strong>
              <span>{effect}</span>
            </article>
          ))}
        </div>
        <div className="mcp-safety-summary">
          <article>
            <CheckCircle2 size={16} />
            <div>
              <strong>Allowed families</strong>
              <span>{profileConfig.allowed_permission_families.join(", ")}</span>
            </div>
          </article>
          <article>
            <Lock size={16} />
            <div>
              <strong>Blocked families</strong>
              <span>{profileConfig.blocked_permission_families.join(", ") || "none"}</span>
            </div>
          </article>
        </div>
        <div className="mcp-inline-guide">
          <Lock size={16} />
          <div>
            <strong>Blocked families are unavailable tools</strong>
            <span>destructive means delete, reset or destructive admin operations. In this profile those tools are hidden or rejected before the AI can call the backend.</span>
          </div>
        </div>
        {delegatedLocalBrain && !registryWriteAllowed ? (
          <div className="mcp-env-error">
            <Lock size={14} />
            <span>{brainScopeLabel(draft, effectiveBrainId)} needs registry_write because the AI must call ensure_brain before memory calls.</span>
          </div>
        ) : null}
        <div className="mcp-secret-inline mcp-secret-status-only">
          <KeyRound size={16} />
          <div>
            <strong>Backend provider key</strong>
            <p>{backendProviderLabel(backendEnvState, providerKey)}</p>
          </div>
        </div>
      </div>
    );
  }

  const clientInstructions = clientSetupInstructions(draft.clientKind);
  const ClientInstructionIcon = clientInstructions.icon;
  const applyConfigReady = clientConfigSafety.applyEnabled && !clientConfigSafety.applyBlocked;
  return (
    <div className="mcp-step-body">
      <StepOutcome icon={Terminal} title="Run the real connection sequence" detail="Docker already runs AGVM API/UI. This step replaces the AI app's MCP server block with a Docker-based bridge config, then you restart the app and send the first prompt." />
      <div className="mcp-connect-client-section">
        <div className="mcp-client-checklist-head">
          <strong>Choose the app you will configure</strong>
          <span>This decides which config format appears on the right. AGVM cannot write into Codex, Claude or Cursor automatically.</span>
        </div>
        <SegmentedControl density="normal" label="AI client" onChange={(clientKind) => onDraftChange({ clientKind })} options={clientOptions} value={draft.clientKind} />
        <div className="mcp-client-action-panel">
          <ClientInstructionIcon size={17} />
          <div>
            <strong>{clientInstructions.title}</strong>
            <span>{clientInstructions.summary}</span>
          </div>
        </div>
        <div className="mcp-app-step-list" aria-label={`${clientLabel(draft.clientKind)} setup steps`}>
          {clientInstructions.steps.map((step, index) => (
            <article key={step}>
              <strong>{index + 1}</strong>
              <span>{step}</span>
            </article>
          ))}
        </div>
        <div className="mcp-config-cleanup-panel" aria-label="Old config cleanup rules">
          <Info size={16} />
          <div>
            <strong>Replace the old AGVM block. Do not merge it.</strong>
            <span>Delete the existing agvm-local-memory-os server entry first, then paste the generated Client config. The correct Docker config uses command=docker and must not contain cwd, AGVM_MCP_CONFIG or OPENAI_API_KEY.</span>
          </div>
        </div>
        <div className={`mcp-apply-config-panel ${applyConfigReady ? "ready" : "blocked"}`} aria-label="Apply permission in copied config">
          {applyConfigReady ? <CheckCircle2 size={16} /> : <Lock size={16} />}
          <div>
            <strong>{applyConfigReady ? "Write test config: explicit_apply is enabled" : "This copied config will not expose apply tools"}</strong>
            <span>
              {applyConfigReady
                ? "Client config includes explicit_apply in allowed families and keeps destructive blocked. Tools like write_memory_commit can appear after restart."
                : "For the revolutionary memory write test, use Operator. Preview mode can create previews but cannot persist them, so write_memory_commit stays hidden."}
            </span>
          </div>
        </div>
      </div>
      <div className="mcp-bridge-explainer" aria-label="What is running">
        <article>
          <Server size={16} />
          <div>
            <strong>Already running in Docker</strong>
            <span>The AGVM API/UI are the backend and dashboard. Your browser check talks to this API at {draft.apiBaseUrl.trim() || defaultDraft.apiBaseUrl}.</span>
          </div>
        </article>
        <article>
          <Terminal size={16} />
          <div>
            <strong>Started by the AI app</strong>
            <span>The MCP bridge is a small stdio process. The selected AI app launches it inside {dockerMcpContainerName} with docker exec, so it can translate MCP tool calls into AGVM API requests.</span>
          </div>
        </article>
      </div>
      <div className="mcp-bridge-command-note">
        <div>
          <Terminal size={16} />
          <div>
            <strong>Bridge command embedded in the client config</strong>
            <span>You usually do not run this from the AGVM UI. Codex/Claude/Cursor runs it after you paste the config.</span>
          </div>
        </div>
        <code>docker exec -i {dockerMcpContainerName} python -m agvm_mcp_server</code>
        <button onClick={() => void onCopy("server-command", `docker exec -i ${dockerMcpContainerName} python -m agvm_mcp_server`)} type="button">
          {copiedId === "server-command" ? <CheckCircle2 size={14} /> : <Copy size={14} />}
          <span>{copiedId === "server-command" ? "Copied" : "Copy command"}</span>
        </button>
      </div>
      <div className="mcp-connect-task-list" aria-label="MCP launch sequence">
        <article className={testState === "ready" ? "done" : ""}>
          <strong>1</strong>
          <div>
            <span>Check Docker API</span>
            <em>Confirms /mcp/contracts is reachable from this browser.</em>
          </div>
        </article>
        <article>
          <strong>2</strong>
          <div>
            <span>Replace client config</span>
            <em>Delete the old agvm-local-memory-os entry, then paste the Client config tab into {clientLabel(draft.clientKind)}.</em>
          </div>
        </article>
        <article>
          <strong>3</strong>
          <div>
            <span>Reload or restart the app</span>
            <em>The app starts the bridge inside the running AGVM Docker container and lists AGVM tools.</em>
          </div>
        </article>
        <article>
          <strong>4</strong>
          <div>
            <span>Send first prompt</span>
            <em>Copy the Prompt tab so the AI calls get_agvm_usage_guide first.</em>
          </div>
        </article>
      </div>
      <div className="mcp-connect-actions">
        <button className={testState === "ready" ? "mcp-check-ready" : testState === "failed" ? "mcp-check-failed" : ""} onClick={() => void onRunLocalCheck()} type="button">
          <PlayCircle size={15} />
          <span>{testState === "running" ? "Checking..." : testState === "ready" ? "Config ready" : testState === "failed" ? "Retry check" : "Check config"}</span>
        </button>
        {applyConfigReady ? (
          <button className="mcp-check-ready" onClick={() => void onCopy("client", clientConfig)} type="button">
            {copiedId === "client" ? <CheckCircle2 size={15} /> : <Copy size={15} />}
            <span>{copiedId === "client" ? "Operator copied" : "Copy Operator config"}</span>
          </button>
        ) : (
          <button className="mcp-operator-copy" onClick={() => void onCopyOperatorClientConfig()} type="button">
            {copiedId === "client" ? <CheckCircle2 size={15} /> : <ShieldCheck size={15} />}
            <span>{copiedId === "client" ? "Operator copied" : "Enable apply + copy"}</span>
          </button>
        )}
        <button onClick={onClear} type="button">
          <Trash2 size={15} />
          <span>Reset flow</span>
        </button>
      </div>
      {copyError ? (
        <div className="mcp-env-error">
          <Lock size={14} />
          <span>{copyError}</span>
        </div>
      ) : null}
      {checkResult ? (
        <div className={`mcp-check-result mcp-check-result-${testState}`}>
          {testState === "ready" ? <CheckCircle2 size={15} /> : testState === "failed" ? <Lock size={15} /> : <PlayCircle size={15} />}
          <div>
            <strong>{checkResult.title}</strong>
            <span>{checkResult.detail}</span>
          </div>
        </div>
      ) : null}
      <div className="mcp-inline-guide">
        <Clipboard size={16} />
        <div>
          <strong>Use the drawer tabs on the right</strong>
          <span>Client config is the only block you paste into the AI app. Env is for debugging/manual runs. Advanced JSON is optional and is not referenced by the generated Docker config.</span>
        </div>
      </div>
    </div>
  );
}

function StepOutcome({ detail, icon: Icon, title }: { detail: string; icon: LucideIcon; title: string }) {
  return (
    <div className="mcp-step-outcome">
      <Icon size={16} />
      <div>
        <strong>{title}</strong>
        <span>{detail}</span>
      </div>
    </div>
  );
}

function EnvChecklist({
  backendEnvState,
  draft,
  effectiveBrainId,
  envSession,
  providerKey,
}: {
  backendEnvState: BackendEnvState;
  draft: McpSetupDraft;
  effectiveBrainId: string;
  envSession: EnvSessionState;
  providerKey: string;
}) {
  const rows = [
    {
      detail: backendEnvState.configured
        ? "Saved in the AGVM API managed env; survives Docker container restarts."
        : providerKey
          ? "Ready to save. Click Save key to backend to make it persistent."
          : "Required by the AGVM API backend before LLM calls work.",
      label: "OPENAI_API_KEY",
      ready: backendEnvState.configured || Boolean(providerKey),
      value: backendEnvState.configured ? "configured" : providerKey ? "ready to save" : "not saved",
    },
    {
      detail: "Used by the MCP bridge to reach the AGVM API.",
      label: "AGVM_API_BASE_URL",
      ready: Boolean(draft.apiBaseUrl.trim()),
      value: draft.apiBaseUrl.trim() || "missing",
    },
    {
      detail: "Cloud/platform token is not required for this local setup and is not exposed yet.",
      label: "Runtime mode",
      ready: true,
      value: "local self-hosted",
    },
  ];
  const readyCount = rows.filter((row) => row.ready).length;
  const tokenState = backendEnvState.configured ? "backend configured" : providerKey ? "ready to save" : "token missing";
  const importedState = envSession.importedAt ? `Imported ${envSession.importedLabel || ".env"} at ${envSession.importedAt}` : "No file import required";
  return (
    <div className="mcp-env-status-strip" aria-label="Loaded environment status">
      <CheckCircle2 size={15} />
      <div>
        <strong>{envSession.importedAt ? `Loaded ${envSession.importedLabel || ".env"}` : "Safe fields prefilled"}</strong>
        <span>{readyCount}/3 ready / {tokenState} / {importedState}</span>
      </div>
    </div>
  );
}

function ArrowConnector() {
  return (
    <i className="mcp-connect-arrow" aria-hidden="true">
      <ArrowRight size={13} />
    </i>
  );
}

function LiveOutputDrawer({
  activeSnippet,
  activeStep,
  backendEnvState,
  clientConfigSafety,
  copyError,
  copied,
  draft,
  envSession,
  effectiveBrainId,
  onCopy,
  onExplainTopic,
  onSelect,
  providerKey,
  snippet,
  snippets,
  testState,
}: {
  activeSnippet: SnippetKind;
  activeStep: SetupStepId;
  backendEnvState: BackendEnvState;
  clientConfigSafety: ClientConfigSafety;
  copyError: string | null;
  copied: boolean;
  draft: McpSetupDraft;
  envSession: EnvSessionState;
  effectiveBrainId: string;
  onCopy: () => void;
  onExplainTopic: (topic: CoachTopicId) => void;
  onSelect: (snippet: SnippetKind) => void;
  providerKey: string;
  snippet: {
    icon: LucideIcon;
    id: SnippetKind;
    label: string;
    text: string;
    title: string;
  };
  snippets: Array<{
    icon: LucideIcon;
    id: SnippetKind;
    label: string;
    text: string;
    title: string;
  }>;
  testState: TestState;
}) {
  const Icon = snippet.icon;
  const stepMeta = setupSteps.find((step) => step.id === activeStep) || setupSteps[0];
  const StepIcon = stepMeta.icon;
  const showCode = activeStep === "connect";
  const applyConfigReady = clientConfigSafety.applyEnabled && !clientConfigSafety.applyBlocked;
  const copyLabel = activeSnippet === "client" ? (applyConfigReady ? "Copy Operator" : "Copy Preview") : "Copy";
  return (
    <aside className={showCode ? "mcp-live-drawer" : "mcp-live-drawer mcp-live-drawer-guide"} aria-label="Generated setup output">
      <header className="mcp-drawer-head">
        {showCode ? <Icon size={17} /> : <StepIcon size={17} />}
        <div>
          <span>{showCode ? "Live output" : "Step guide"}</span>
          <strong>{showCode ? snippet.title : stepMeta.title}</strong>
        </div>
        {showCode ? (
          <button onClick={onCopy} type="button">
            {copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
            <span>{copied ? "Copied" : copyLabel}</span>
          </button>
        ) : null}
      </header>

      <div className="mcp-live-preview">
        <SetupFact icon={KeyRound} label="Env" value={envStatusLabel(envSession, providerKey, backendEnvState)} />
        <SetupFact icon={Plug} label="Client" value={clientLabel(draft.clientKind)} />
        <SetupFact icon={Database} label="Brain" value={brainScopeLabel(draft, effectiveBrainId)} />
        <SetupFact icon={ShieldCheck} label="Apply" value={applyConfigReady ? "enabled" : "blocked"} />
        <SetupFact icon={PlayCircle} label="Check" value={testLabel(testState)} />
      </div>

      {showCode ? (
        <>
          <div className="mcp-output-tabs" role="tablist" aria-label="Generated setup snippets">
            {snippets.map((candidate) => {
              const CandidateIcon = candidate.icon;
              const active = candidate.id === activeSnippet;
              return (
                <button
                  aria-pressed={active}
                  className={active ? "active" : ""}
                  data-snippet-id={candidate.id}
                  key={candidate.id}
                  onClick={() => onSelect(candidate.id)}
                  type="button"
                >
                  <CandidateIcon size={13} />
                  <span>{candidate.label}</span>
                </button>
              );
            })}
          </div>

          {copyError ? (
            <div className="mcp-copy-error">
              <Lock size={13} />
              <span>{copyError}</span>
            </div>
          ) : null}
          <pre className="mcp-output-code">{snippet.text}</pre>
        </>
      ) : (
        <StepGuidePanel onExplainTopic={onExplainTopic} step={stepMeta} />
      )}
    </aside>
  );
}

function StepGuidePanel({ onExplainTopic, step }: { onExplainTopic: (topic: CoachTopicId) => void; step: (typeof setupSteps)[number] }) {
  return (
    <div className="mcp-step-guide-panel">
      <article>
        <span>What clicking this step does</span>
        <strong>{step.goal}</strong>
      </article>
      <article>
        <span>When it is done</span>
        <strong>{step.done}</strong>
      </article>
      <article>
        <span>What changes next</span>
        <strong>{step.hint}</strong>
      </article>
      <div className="mcp-guide-topic-buttons">
        {guideTopicsForStep(step.id).map((topic) => (
          <button key={topic} onClick={() => onExplainTopic(topic)} type="button">
            <Info size={13} />
            <span>{coachTopicShortLabel(topic)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function CoachModal({
  activeStep,
  autoCoach,
  draft,
  effectiveBrainId,
  onClose,
  onDisableAuto,
  onGoToStep,
  onTopic,
  profileConfig,
  testState,
  topic,
}: {
  activeStep: SetupStepId;
  autoCoach: boolean;
  draft: McpSetupDraft;
  effectiveBrainId: string;
  onClose: () => void;
  onDisableAuto: () => void;
  onGoToStep: (step: SetupStepId) => void;
  onTopic: (topic: CoachTopicId) => void;
  profileConfig: ReturnType<typeof permissionProfileConfig>;
  testState: TestState;
  topic: CoachTopicId;
}) {
  const content = coachTopicContent(topic, draft, effectiveBrainId, profileConfig, testState);
  const Icon = content.icon;
  return (
    <div className="mcp-coach-overlay" role="presentation">
      <section aria-labelledby="mcp-coach-title" aria-modal="true" className="mcp-coach-modal" role="dialog">
        <header className="mcp-coach-head">
          <Icon size={19} />
          <div>
            <span>{content.eyebrow}</span>
            <h3 id="mcp-coach-title">{content.title}</h3>
            <p>{content.summary}</p>
          </div>
          <button aria-label="Close guide" onClick={onClose} type="button">
            <X size={16} />
          </button>
        </header>

        <nav className="mcp-coach-step-rail" aria-label="Coach step navigation">
          {setupSteps.map((step) => {
            const StepIcon = step.icon;
            const active = step.id === activeStep;
            return (
              <button className={active ? "active" : ""} key={step.id} onClick={() => onGoToStep(step.id)} type="button">
                <StepIcon size={13} />
                <span>{step.label}</span>
              </button>
            );
          })}
        </nav>

        {content.callout ? (
          <div className="mcp-coach-callout">
            <Info size={15} />
            <strong>{content.callout}</strong>
          </div>
        ) : null}

        <div className={`mcp-coach-card-grid ${content.layout === "flow" ? "flow" : ""}`}>
          {content.cards.map((card) => (
            <article key={card.label}>
              <span>{card.label}</span>
              <strong>{card.body}</strong>
            </article>
          ))}
        </div>

        {topic === "permissions" ? (
          <div className="mcp-coach-family-grid" aria-label="Permission family glossary">
            {permissionFamilyRows().map((row) => (
              <article key={row.family} className={profileConfig.blocked_permission_families.includes(row.family) ? "blocked" : profileConfig.allowed_permission_families.includes(row.family) ? "allowed" : ""}>
                <span>{row.family}</span>
                <strong>{row.meaning}</strong>
              </article>
            ))}
          </div>
        ) : null}

        {content.relatedTopics?.length ? (
          <div className="mcp-coach-related">
            <span>Need clarity on</span>
            <div>
              {content.relatedTopics.map((relatedTopic) => (
                <button key={relatedTopic} onClick={() => onTopic(relatedTopic)} type="button">
                  <Info size={13} />
                  <span>{coachTopicShortLabel(relatedTopic)}</span>
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <footer className="mcp-coach-footer">
          <div>
            <button onClick={onDisableAuto} type="button">{autoCoach ? "Stop auto popups" : "Auto popups off"}</button>
          </div>
          <button className="primary" onClick={onClose} type="button">Got it</button>
        </footer>
      </section>
    </div>
  );
}

function AdvancedSetupDetails({
  backendEnvState,
  draft,
  effectiveBrainId,
  profileConfig,
  providerKey,
}: {
  backendEnvState: BackendEnvState;
  draft: McpSetupDraft;
  effectiveBrainId: string;
  profileConfig: ReturnType<typeof permissionProfileConfig>;
  providerKey: string;
}) {
  return (
    <details className="mcp-advanced-details">
      <summary>
        <span>
          <ChevronDown size={15} />
          Advanced details
        </span>
        <em>Runtime guide, vault direction and exact safety contract</em>
      </summary>
      <div className="mcp-details-grid">
        <article>
          <Link size={16} />
          <div>
            <strong>Agent flow</strong>
            <p>First call `get_agvm_usage_guide`, then resolve the brain, then call `retrieve_context` with a concrete query.</p>
          </div>
        </article>
        <article>
          <Cloud size={16} />
          <div>
            <strong>Platform automation</strong>
            <p>The future paid platform should store user keys in a backend vault and issue scoped setup bundles automatically.</p>
          </div>
        </article>
        <article>
          <ShieldCheck size={16} />
          <div>
            <strong>Current contract</strong>
            <p>
              {profileLabel(draft.permissionProfile)} for {brainScopeLabel(draft, effectiveBrainId)}. Blocked: {profileConfig.blocked_permission_families.join(", ") || "none"}.
            </p>
          </div>
        </article>
        <article>
          <KeyRound size={16} />
          <div>
            <strong>Secret state</strong>
            <p>{backendEnvState.configured ? "Backend provider key is configured." : providerKey.trim() ? "A key is present only in this browser session until saved." : "No full provider key is stored by this UI."}</p>
          </div>
        </article>
      </div>
    </details>
  );
}

function SetupFact({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <div className="mcp-setup-fact">
      <Icon size={14} />
      <span>{label}</span>
      <strong title={value}>{value}</strong>
    </div>
  );
}

function coachTopicContent(
  topic: CoachTopicId,
  draft: McpSetupDraft,
  effectiveBrainId: string,
  profileConfig: ReturnType<typeof permissionProfileConfig>,
  testState: TestState,
): {
  callout?: string;
  cards: Array<{ body: string; label: string }>;
  eyebrow: string;
  icon: LucideIcon;
  layout?: "flow";
  relatedTopics?: CoachTopicId[];
  summary: string;
  title: string;
} {
  if (topic === "overview") {
    return {
      eyebrow: "Guided flow",
      icon: Plug,
      title: "What this setup will do",
      summary: "This wizard prepares one AI client to use AGVM as external memory through the local MCP bridge.",
      callout: "The AI reads the MCP contract and usage guide after it connects; this UI prepares the runtime and safety boundary.",
      layout: "flow",
      cards: [
        { label: "1. Env", body: "Review API URL, then save the provider key once into the AGVM API managed env." },
        { label: "2. Brain", body: "Choose fixed brain, AI-resolved brain, or AI-created local brain." },
        { label: "3. Safety", body: "Choose whether the AI can only read, preview learning, or call apply tools after approval." },
        { label: "4. Connect", body: "Choose Codex, Claude, Cursor or generic MCP, run a real API registry check, then copy config and prompt." },
      ],
    };
  }
  if (topic === "env") {
    return {
      eyebrow: "Step 1",
      icon: KeyRound,
      title: "Save provider key",
      summary: "Paste a .env block or load a file so the setup can fill safe runtime fields.",
      callout: "The backend save writes a managed env file in the API data volume; Save setup stores only non-secret UI choices.",
      cards: [
        { label: "OPENAI_API_KEY", body: "Needed by the AGVM API backend for LLM calls. The MCP bridge should not become the durable secret store." },
        { label: "AGVM_API_BASE_URL", body: `Used by the bridge to call the backend. Current value: ${draft.apiBaseUrl || defaultDraft.apiBaseUrl}.` },
        { label: "Cloud platform", body: "No AGVM platform token or Google login is active today. This local setup only needs API URL and backend provider key." },
      ],
    };
  }
  if (topic === "client") {
    return {
      eyebrow: "Connect",
      icon: Plug,
      title: "Choose the AI client config",
      summary: "The client choice changes only the generated MCP config format and the app-specific setup steps in Connect.",
      cards: [
        { label: "Codex", body: "Replace the entire agvm-local-memory-os TOML block in Codex config.toml, then reload MCP servers or start a new chat." },
        { label: "Claude / Cursor", body: "Replace the existing agvm-local-memory-os mcpServers entry in that app's MCP configuration surface, then restart/reload the app." },
        { label: "What the AI sees", body: "After the app starts the bridge, the AI receives tool schemas and should first call get_agvm_usage_guide." },
      ],
    };
  }
  if (topic === "brain" || topic === "hosted") {
    return {
      eyebrow: "Step 2",
      icon: Database,
      title: "Pick the local memory policy",
      summary: "This decides how the AI gets a local brain_id. It is separate from provider keys, cloud login and the dashboard active brain.",
      callout: "AI creates is the recommended first real test because it proves the client can call ensure_brain and then reuse the returned brain_id.",
      relatedTopics: ["apply"],
      cards: [
        { label: "Fixed", body: `Bridge injects brain_id=${effectiveBrainId} when the AI omits brain_id. UI active brain changes later do not change an already copied config.` },
        { label: "AI creates", body: "Bridge does not inject a brain. The AI must call ensure_brain with create_if_missing=true, then reuse the returned brain_id." },
        { label: "AI picks", body: "Bridge does not inject a brain. The AI must call list_brains or ensure_brain with create_if_missing=false, then reuse the chosen brain_id." },
      ],
    };
  }
  if (topic === "safety" || topic === "permissions") {
    return {
      eyebrow: topic === "permissions" ? "Permission glossary" : "Step 3",
      icon: ShieldCheck,
      title: "Understand allowed and blocked families",
      summary: `${profileLabel(draft.permissionProfile)} allows ${profileConfig.allowed_permission_families.join(", ")} and blocks ${profileConfig.blocked_permission_families.join(", ") || "nothing"}.`,
      callout: "Blocked means the local MCP bridge hides or rejects that family before the backend tool call.",
      relatedTopics: ["apply"],
      cards: [
        { label: "Preview mode", body: "Lets the AI form proposals and preview memory changes without mutating the graph." },
        { label: "Operator mode", body: "Exposes explicit_apply tools, but backend apply endpoints still require confirm_apply and valid preview/proposal data." },
        { label: "Destructive", body: "Delete/reset/admin destructive operations stay blocked by default even in operator mode." },
      ],
    };
  }
  if (topic === "apply") {
    return {
      eyebrow: "Apply policy",
      icon: ShieldCheck,
      title: "Can the AI apply by itself?",
      summary: "It can call apply tools only when the MCP profile exposes explicit_apply and the backend receives confirm_apply=true.",
      callout: "For a real test where the AI may write, choose Operator. For safe learning only, keep Preview.",
      relatedTopics: ["permissions"],
      cards: [
        { label: "Preview first", body: "Tools like grow_source_preview, sleep_preview and evolve_preview produce candidate changes without mutation." },
        { label: "Explicit apply", body: "Tools like grow_source_apply, grow_apply, write_memory_commit, sleep_apply and evolve_apply are in explicit_apply." },
        { label: "Backend gate", body: "The API rejects apply without confirm_apply=true and may still block unsafe previews or incomplete proposal selections." },
      ],
    };
  }
  return {
    eyebrow: "Step 4",
    icon: Terminal,
    title: "Connect and test",
    summary: "The final step checks the AGVM API contract registry, then shows the exact config, env, local config and first prompt.",
    callout: testState === "ready" ? "The API registry check passed. The next real test is pasting the generated config into the selected AI client so it starts the stdio bridge." : "Check config only verifies Docker/API readiness. It does not connect Codex, Claude or Cursor yet.",
    relatedTopics: ["apply"],
    cards: [
      { label: "Check config", body: "Verifies that the browser can reach the AGVM API and read MCP tools from Docker." },
      { label: "Start bridge", body: "The AI client runs docker exec into the AGVM API container and starts python -m agvm_mcp_server there. That process is the MCP adapter, not the dashboard UI." },
      { label: "Clean replace", body: "Remove stale cwd, AGVM_MCP_CONFIG, OPENAI_API_KEY and Python-host entries by replacing the whole agvm-local-memory-os block." },
      { label: "First prompt", body: "Tells the AI to use AGVM as external persistent memory and to call get_agvm_usage_guide first." },
    ],
  };
}

function guideTopicsForStep(step: SetupStepId): CoachTopicId[] {
  if (step === "brain") return ["brain"];
  if (step === "safety") return ["safety", "permissions", "apply"];
  if (step === "connect") return ["connect", "client", "apply"];
  return [step];
}

function coachTopicShortLabel(topic: CoachTopicId) {
  if (topic === "overview") return "Flow";
  if (topic === "hosted") return "Hosted";
  if (topic === "permissions") return "Families";
  if (topic === "apply") return "Apply";
  const step = setupSteps.find((candidate) => candidate.id === topic);
  return step?.label || topic;
}

function permissionFamilyRows() {
  return [
    { family: "read_only", meaning: "Retrieve and inspect memory without writes." },
    { family: "read_only_export", meaning: "Expose bounded export/read packages." },
    { family: "registry_write", meaning: "Create, ensure or select brains." },
    { family: "preview_only", meaning: "Build learning or maintenance previews without mutation." },
    { family: "explicit_apply", meaning: "Apply writes after explicit confirmation and backend guards." },
    { family: "destructive", meaning: "Delete, reset or destructive admin operations." },
  ];
}

function loadDraft(activeBrainId: string): McpSetupDraft {
  if (typeof window === "undefined") return { ...defaultDraft, brainDisplayName: defaultBrainDisplayName(activeBrainId) };
  try {
    const stored = JSON.parse(window.localStorage.getItem(storageKey) || "{}") as Partial<McpSetupDraft>;
    const storedBrainPolicy = stored.brainPolicy || (stored.brainId ? "fixed" : defaultDraft.brainPolicy);
    return {
      ...defaultDraft,
      ...stored,
      brainDisplayName: stored.brainDisplayName || defaultBrainDisplayName(activeBrainId),
      brainId: stored.brainId || (storedBrainPolicy === "fixed" ? activeBrainId : defaultDraft.brainId),
      brainPolicy: storedBrainPolicy,
      runtimeScope: "local",
    };
  } catch {
    return { ...defaultDraft, brainDisplayName: defaultBrainDisplayName(activeBrainId) };
  }
}

function permissionProfileConfig(profile: PermissionProfile) {
  if (profile === "read_only_recall") {
    return {
      read_only: true,
      allowed_permission_families: ["read_only", "read_only_export"],
      blocked_permission_families: ["registry_write", "preview_only", "explicit_apply", "destructive"],
    };
  }
  if (profile === "agent_onboarding") {
    return {
      read_only: false,
      allowed_permission_families: ["read_only", "read_only_export", "registry_write"],
      blocked_permission_families: ["preview_only", "explicit_apply", "destructive"],
    };
  }
  if (profile === "full_local_operator") {
    return {
      read_only: false,
      allowed_permission_families: ["read_only", "read_only_export", "registry_write", "preview_only", "explicit_apply"],
      blocked_permission_families: ["destructive"],
    };
  }
  return {
    read_only: false,
    allowed_permission_families: ["read_only", "read_only_export", "registry_write", "preview_only"],
    blocked_permission_families: ["explicit_apply", "destructive"],
  };
}

function buildLocalConfig(draft: McpSetupDraft, effectiveBrainId: string, profileConfig: ReturnType<typeof permissionProfileConfig>) {
  const fixedLocalBrain = draft.runtimeScope === "local" && draft.brainPolicy === "fixed";
  const delegatedLocalBrain = draft.runtimeScope === "local" && draft.brainPolicy !== "fixed";
  return {
    api_base_url: draft.apiBaseUrl.trim() || defaultDraft.apiBaseUrl,
    active_brain_id: fixedLocalBrain ? effectiveBrainId : null,
    default_brain_id: fixedLocalBrain ? effectiveBrainId : null,
    brain_policy: draft.runtimeScope === "local" ? draft.brainPolicy : "fixed",
    brain_id_hint: delegatedLocalBrain ? draft.brainId.trim() || null : null,
    brain_display_name: delegatedLocalBrain ? draft.brainDisplayName.trim() || defaultDraft.brainDisplayName : null,
    brain_purpose: delegatedLocalBrain ? draft.brainPurpose.trim() || null : null,
    tenant_id: draft.runtimeScope === "hosted" ? draft.tenantId.trim() : null,
    organization_id: draft.runtimeScope === "hosted" ? draft.organizationId.trim() : null,
    user_id: draft.runtimeScope === "hosted" ? draft.userId.trim() : null,
    environment_id: draft.environmentId.trim() || defaultDraft.environmentId,
    request_timeout_seconds: 180,
    tool_permissions: {
      enabled_tools: ["*"],
      disabled_tools: [],
      ...profileConfig,
    },
    module_access: {
      visibility_policy: localModuleVisibilityPolicy,
      status_source: "local_license_supervisor",
      license_state_path: null,
    },
  };
}

function buildEnvBlock(draft: McpSetupDraft, effectiveBrainId: string, profileConfig: ReturnType<typeof permissionProfileConfig>, providerKey: string) {
  const lines = [
    `AGVM_API_BASE_URL=${draft.apiBaseUrl.trim() || defaultDraft.apiBaseUrl}`,
    `AGVM_MCP_ALLOWED_PERMISSION_FAMILIES=${profileConfig.allowed_permission_families.join(",")}`,
    `AGVM_MCP_BLOCKED_PERMISSION_FAMILIES=${profileConfig.blocked_permission_families.join(",")}`,
    `AGVM_MCP_READ_ONLY=${profileConfig.read_only ? "true" : "false"}`,
    `AGVM_MCP_MODULE_VISIBILITY_POLICY=${localModuleVisibilityPolicy}`,
  ];
  if (draft.runtimeScope === "local") {
    lines.push(`AGVM_MCP_BRAIN_POLICY=${draft.brainPolicy}`);
    if (draft.brainPolicy === "fixed") {
      lines.push(`AGVM_MCP_BRAIN_ID=${effectiveBrainId}`);
    } else {
      if (draft.brainId.trim()) lines.push(`AGVM_MCP_BRAIN_ID_HINT=${draft.brainId.trim()}`);
      lines.push(`AGVM_MCP_BRAIN_DISPLAY_NAME=${draft.brainDisplayName.trim() || defaultDraft.brainDisplayName}`);
      if (draft.brainPurpose.trim()) lines.push(`AGVM_MCP_BRAIN_PURPOSE=${draft.brainPurpose.trim()}`);
    }
  } else {
    lines.push(`AGVM_MCP_TENANT_ID=${draft.tenantId.trim() || "tenant_id"}`);
    lines.push(`AGVM_MCP_ORGANIZATION_ID=${draft.organizationId.trim() || "organization_id"}`);
    lines.push(`AGVM_MCP_USER_ID=${draft.userId.trim() || "user_id"}`);
    lines.push(`AGVM_MCP_ENVIRONMENT_ID=${draft.environmentId.trim() || defaultDraft.environmentId}`);
    lines.push("AGVM_HOSTED_ACCESS_TOKEN=<future platform vault token>");
  }
  return lines.join("\n");
}

function buildBackendEnvBlock(draft: McpSetupDraft, effectiveBrainId: string, profileConfig: ReturnType<typeof permissionProfileConfig>, providerKey: string) {
  const lines = [
    `OPENAI_API_KEY=${providerKey || "<paste-openai-key>"}`,
    "AGVM_LLM_ENABLED=true",
    `AGVM_API_BASE_URL=${draft.apiBaseUrl.trim() || defaultDraft.apiBaseUrl}`,
    `AGVM_MCP_BRAIN_POLICY=${draft.runtimeScope === "local" ? draft.brainPolicy : "fixed"}`,
    `AGVM_MCP_READ_ONLY=${profileConfig.read_only ? "true" : "false"}`,
    `AGVM_MCP_ALLOWED_PERMISSION_FAMILIES=${profileConfig.allowed_permission_families.join(",")}`,
    `AGVM_MCP_BLOCKED_PERMISSION_FAMILIES=${profileConfig.blocked_permission_families.join(",")}`,
    `AGVM_MCP_MODULE_VISIBILITY_POLICY=${localModuleVisibilityPolicy}`,
  ];
  if (draft.runtimeScope === "local" && draft.brainPolicy === "fixed") {
    lines.push(`AGVM_MCP_BRAIN_ID=${effectiveBrainId}`);
  }
  if (draft.runtimeScope === "local" && draft.brainPolicy !== "fixed") {
    if (draft.brainId.trim()) lines.push(`AGVM_MCP_BRAIN_ID_HINT=${draft.brainId.trim()}`);
    lines.push(`AGVM_MCP_BRAIN_DISPLAY_NAME=${draft.brainDisplayName.trim() || defaultDraft.brainDisplayName}`);
    if (draft.brainPurpose.trim()) lines.push(`AGVM_MCP_BRAIN_PURPOSE=${draft.brainPurpose.trim()}`);
  }
  if (draft.runtimeScope === "hosted") {
    lines.push(`AGVM_MCP_TENANT_ID=${draft.tenantId.trim() || "tenant_id"}`);
    lines.push(`AGVM_MCP_ORGANIZATION_ID=${draft.organizationId.trim() || "organization_id"}`);
    lines.push(`AGVM_MCP_USER_ID=${draft.userId.trim() || "user_id"}`);
    lines.push(`AGVM_MCP_ENVIRONMENT_ID=${draft.environmentId.trim() || defaultDraft.environmentId}`);
  }
  return lines.join("\n");
}

function buildClientConfig(draft: McpSetupDraft, envBlock: string) {
  const env = buildGeneratedClientEnv(envBlock);
  const bridgeArgs = buildDockerBridgeArgs(env);
  if (draft.clientKind === "codex") {
    return buildCodexTomlConfig(bridgeArgs);
  }
  const body = {
    mcpServers: {
      "agvm-local-memory-os": {
        command: "docker",
        args: bridgeArgs,
      },
    },
  };
  if (draft.clientKind === "generic") {
    return [
      "Transport: stdio",
      "Command: docker",
      `Args: ${bridgeArgs.join(" ")}`,
      "",
      JSON.stringify(body.mcpServers["agvm-local-memory-os"], null, 2),
    ].join("\n");
  }
  return JSON.stringify(body, null, 2);
}

function buildGeneratedClientEnv(envBlock: string) {
  return Object.fromEntries(
    envBlock
      .split("\n")
      .map((line) => line.split("="))
      .map(([key, ...valueParts]) => [key.trim(), valueParts.join("=").trim()] as const)
      .filter(([key, value]) => Boolean(key) && !generatedClientEnvBlocklist.has(key) && !isPlaceholderEnvValue(value)),
  );
}

function inspectClientConfigSafety(config: string): ClientConfigSafety {
  const allowedFamilies = splitPermissionFamilies(readGeneratedEnvValueFromConfig(config, "AGVM_MCP_ALLOWED_PERMISSION_FAMILIES"));
  const blockedFamilies = splitPermissionFamilies(readGeneratedEnvValueFromConfig(config, "AGVM_MCP_BLOCKED_PERMISSION_FAMILIES"));
  return {
    allowedFamilies,
    applyBlocked: blockedFamilies.includes("explicit_apply"),
    applyEnabled: allowedFamilies.includes("explicit_apply"),
    blockedFamilies,
  };
}

function readGeneratedEnvValueFromConfig(config: string, key: string) {
  const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escapedKey}=([^"\\]\\n\\r]+)`).exec(config);
  return match?.[1]?.trim() || "";
}

function splitPermissionFamilies(value: string | undefined) {
  return String(value || "")
    .split(",")
    .map((family) => family.trim())
    .filter(Boolean);
}

function isPlaceholderEnvValue(value: string) {
  return /^<[^>]+>$/.test(String(value || "").trim());
}

function buildDockerBridgeArgs(env: Record<string, string>) {
  const args = ["exec", "-i"];
  for (const [key, value] of Object.entries(env)) {
    args.push("-e", `${key}=${value}`);
  }
  args.push(dockerMcpContainerName, "python", "-m", "agvm_mcp_server");
  return args;
}

function buildCodexTomlConfig(args: string[]) {
  return [
    "[mcp_servers.\"agvm-local-memory-os\"]",
    'command = "docker"',
    `args = [${args.map(tomlString).join(", ")}]`,
    "startup_timeout_sec = 30",
    "tool_timeout_sec = 180",
  ].join("\n");
}

function tomlString(value: string) {
  return `"${String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

async function writeClipboardText(value: string) {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Fall through to the legacy selection copy path.
    }
  }
  if (typeof document === "undefined") {
    throw new Error("Clipboard unavailable");
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    const copied = document.execCommand("copy");
    if (!copied) throw new Error("Clipboard copy denied");
  } finally {
    document.body.removeChild(textarea);
  }
}

function buildFirstPrompt(draft: McpSetupDraft, effectiveBrainId: string) {
  const lines = [
    "Use AGVM as external persistent memory, not as model-internal memory.",
    "First call get_agvm_usage_guide.",
    "For recall, call retrieve_context with a concrete natural-language query_text.",
    "For learning, call preview tools first.",
    "If a Grow/Maintain module tool is missing or blocked, report that local Pro module access is not active for that module instead of inventing a workaround.",
  ];
  if (draft.runtimeScope === "hosted") {
    lines.splice(2, 0, "Use the hosted tenant/user/environment scope from the MCP config; do not invent tenant ids.");
  } else if (draft.brainPolicy === "fixed") {
    lines.splice(2, 0, `Use brain_id=${effectiveBrainId} on retrieval and preview calls unless the user explicitly asks for another brain.`);
  } else if (draft.brainPolicy === "ai_resolve_existing") {
    const target = draft.brainDisplayName.trim() || defaultDraft.brainDisplayName;
    lines.splice(
      2,
      0,
      `Resolve an existing brain first: call list_brains, then call ensure_brain with display_name="${target}", create_if_missing=false and activation_policy=return_only, or choose a matching existing brain_id from list_brains.`,
      "After resolution, pass that concrete brain_id on every retrieval, preview or apply call.",
    );
  } else {
    const target = draft.brainDisplayName.trim() || defaultDraft.brainDisplayName;
    const hint = draft.brainId.trim() ? ` and brain_id="${draft.brainId.trim()}"` : "";
    lines.splice(
      2,
      0,
      `Create or resolve the MCP brain first: call ensure_brain with display_name="${target}"${hint}, create_if_missing=true and activation_policy=return_only.`,
      "After ensure_brain returns, pass the returned brain_id on every retrieval, preview or apply call.",
    );
  }
  if (draft.permissionProfile === "full_local_operator") {
    lines.push("If the user explicitly approves an apply action, call the matching apply tool with confirm_apply=true and include the preview/proposal identifiers required by that tool.");
  } else {
    lines.push("Do not call apply tools in this profile; use preview/status tools only.");
  }
  return lines.join("\n");
}

function snippetForStep(step: SetupStepId): SnippetKind {
  if (step === "env") return "env";
  if (step === "brain") return "env";
  if (step === "safety") return "config";
  if (step === "connect") return "client";
  return "client";
}

function isStepComplete(
  step: SetupStepId,
  index: number,
  activeIndex: number,
  draft: McpSetupDraft,
  envSession: EnvSessionState,
  effectiveBrainId: string,
  providerKey: string,
  testState: TestState,
) {
  if (index < activeIndex) return true;
  if (step === "env") return Boolean(draft.apiBaseUrl.trim() || defaultDraft.apiBaseUrl);
  if (step === "brain") return isBrainStepReady(draft, effectiveBrainId, permissionProfileConfig(draft.permissionProfile));
  if (step === "safety") return Boolean(draft.permissionProfile) && isBrainStepReady(draft, effectiveBrainId, permissionProfileConfig(draft.permissionProfile));
  return testState === "ready";
}

function stepPreview(step: SetupStepId, draft: McpSetupDraft, envSession: EnvSessionState, effectiveBrainId: string, providerKey: string, backendEnvState: BackendEnvState, testState: TestState) {
  if (step === "env") return envStatusLabel(envSession, providerKey, backendEnvState);
  if (step === "brain") return draft.runtimeScope === "hosted" ? `${draft.tenantId || "tenant"} / ${draft.userId || "user"}` : brainScopeLabel(draft, effectiveBrainId);
  if (step === "safety") return profileLabel(draft.permissionProfile);
  return testLabel(testState);
}

function parseEnvText(text: string): { error: string | null; values: Record<string, string> } {
  const values: Record<string, string> = {};
  const lines = String(text || "").split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const rawLine = lines[index].trim();
    if (!rawLine || rawLine.startsWith("#")) continue;
    const line = rawLine.startsWith("export ") ? rawLine.slice(7).trim() : rawLine;
    const match = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line);
    if (!match) {
      return { error: `Line ${index + 1} is not KEY=value format.`, values: {} };
    }
    const [, key, rawValue] = match;
    values[key] = unquoteEnvValue(rawValue.trim());
  }
  if (!Object.keys(values).length) {
    return { error: "Paste at least one KEY=value line before using this env block.", values: {} };
  }
  return { error: null, values };
}

function unquoteEnvValue(value: string) {
  if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
    return value.slice(1, -1);
  }
  return value;
}

function draftPatchFromEnv(values: Record<string, string>): Partial<McpSetupDraft> {
  const patch: Partial<McpSetupDraft> = {};
  if (values.AGVM_API_BASE_URL) patch.apiBaseUrl = values.AGVM_API_BASE_URL;
  const permissionProfile = permissionProfileFromEnv(values);
  if (permissionProfile) patch.permissionProfile = permissionProfile;
  if (isBrainPolicy(values.AGVM_MCP_BRAIN_POLICY)) {
    patch.brainPolicy = values.AGVM_MCP_BRAIN_POLICY;
    patch.runtimeScope = "local";
  }
  if (values.AGVM_MCP_BRAIN_ID) {
    patch.brainId = values.AGVM_MCP_BRAIN_ID;
    patch.brainPolicy = "fixed";
    patch.runtimeScope = "local";
  }
  if (values.AGVM_MCP_BRAIN_ID_HINT) {
    patch.brainId = values.AGVM_MCP_BRAIN_ID_HINT;
    patch.runtimeScope = "local";
  }
  if (values.AGVM_MCP_BRAIN_DISPLAY_NAME) patch.brainDisplayName = values.AGVM_MCP_BRAIN_DISPLAY_NAME;
  if (values.AGVM_MCP_BRAIN_PURPOSE) patch.brainPurpose = values.AGVM_MCP_BRAIN_PURPOSE;
  if (values.AGVM_MCP_TENANT_ID || values.AGVM_MCP_USER_ID || values.AGVM_HOSTED_ACCESS_TOKEN) {
    patch.runtimeScope = "hosted";
  }
  if (values.AGVM_MCP_TENANT_ID) patch.tenantId = values.AGVM_MCP_TENANT_ID;
  if (values.AGVM_MCP_ORGANIZATION_ID) patch.organizationId = values.AGVM_MCP_ORGANIZATION_ID;
  if (values.AGVM_MCP_USER_ID) patch.userId = values.AGVM_MCP_USER_ID;
  if (values.AGVM_MCP_ENVIRONMENT_ID) patch.environmentId = values.AGVM_MCP_ENVIRONMENT_ID;
  return patch;
}

function permissionProfileFromEnv(values: Record<string, string>): PermissionProfile | null {
  const allowed = splitPermissionFamilies(values.AGVM_MCP_ALLOWED_PERMISSION_FAMILIES);
  const blocked = splitPermissionFamilies(values.AGVM_MCP_BLOCKED_PERMISSION_FAMILIES);
  if (!allowed.length && !blocked.length) return null;
  if (allowed.includes("explicit_apply") && !blocked.includes("explicit_apply")) return "full_local_operator";
  if (allowed.includes("preview_only") && !blocked.includes("preview_only")) return "preview_only_learning";
  if (allowed.includes("registry_write") && !blocked.includes("registry_write")) return "agent_onboarding";
  return "read_only_recall";
}

function envStatusLabel(envSession: EnvSessionState, providerKey: string, backendEnvState: BackendEnvState) {
  if (backendEnvState.loading) return "checking backend";
  if (backendEnvState.configured) return "key saved";
  if (providerKey) return "key ready to save";
  if (backendEnvState.error) return "backend unavailable";
  if (envSession.importedAt) return "env imported";
  return "fields ready";
}

function backendProviderLabel(backendEnvState: BackendEnvState, providerKey: string) {
  if (backendEnvState.loading) return "Checking server-side key storage...";
  if (backendEnvState.configured) {
    const source = backendEnvState.source === "managed_runtime_env" ? "managed env" : backendEnvState.source === "process_env" ? "Docker/root env" : backendEnvState.source;
    return `Provider key saved via ${source}.`;
  }
  if (backendEnvState.error) return "Provider-key storage is unavailable; retry after the API is running.";
  if (providerKey.trim()) return "Key is in this browser only until you save it to the API service.";
  return "Paste the key here, then save it once to the API service.";
}

function readApiError(payload: SetupEnvStatusPayload, status: number) {
  const detail = payload?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) return JSON.stringify(detail[0]);
  return `HTTP ${status}`;
}

function maskSecret(value: string) {
  const text = String(value || "").trim();
  if (text.length <= 10) return "loaded";
  return `${text.slice(0, 6)}...${text.slice(-4)}`;
}

function redactedEnvText(values: Record<string, string>) {
  return Object.entries(values)
    .map(([key, value]) => `${key}=${isSecretEnvKey(key) ? maskSecret(value) : value}`)
    .join("\n");
}

function isSecretEnvKey(key: string) {
  const normalized = key.toUpperCase();
  return normalized.includes("KEY") || normalized.includes("TOKEN") || normalized.includes("SECRET") || normalized.includes("PASSWORD");
}

function setupStatus(draft: McpSetupDraft, providerKey: string, backendEnvState: BackendEnvState, testState: TestState) {
  if (testState === "ready") return "config ready";
  if (testState === "running") return "checking config";
  if (testState === "failed") return "check failed";
  if (backendEnvState.configured) return "provider key saved";
  if (draft.runtimeScope === "hosted" && (!draft.tenantId.trim() || !draft.userId.trim())) return "hosted scope incomplete";
  if (providerKey.trim()) return "key ready to save";
  return "guided setup";
}

function testLabel(state: TestState) {
  if (state === "running") return "checking";
  if (state === "ready") return "ready";
  if (state === "failed") return "failed";
  return "not checked";
}

function clientLabel(kind: ClientKind) {
  if (kind === "codex") return "Codex";
  if (kind === "claude") return "Claude Desktop";
  if (kind === "cursor") return "Cursor";
  return "Generic MCP";
}

function clientSetupInstructions(kind: ClientKind): { icon: LucideIcon; steps: string[]; summary: string; title: string } {
  if (kind === "claude") {
    return {
      icon: Plug,
      title: "Claude Desktop reads mcpServers JSON.",
      summary: "Open Claude Desktop's MCP/developer config, paste the generated JSON, restart Claude Desktop, then send the first prompt.",
      steps: [
        "Open Claude Desktop settings, then the Developer/MCP config editor.",
        "Delete any existing agvm-local-memory-os entry, then paste the Client config mcpServers JSON.",
        "Save the file and restart Claude Desktop so it starts the AGVM bridge.",
        "Open a new chat, confirm AGVM appears in tools/connectors, then send the Prompt tab text.",
      ],
    };
  }
  if (kind === "cursor") {
    return {
      icon: Code2,
      title: "Cursor uses an MCP server entry.",
      summary: "Open Cursor Settings -> MCP, add or edit a server, paste the generated JSON, then start an Agent chat.",
      steps: [
        "Open Cursor Settings -> MCP.",
        "Add a new MCP server or open the mcp.json editor.",
        "Delete any existing agvm-local-memory-os entry, then paste the Client config entry.",
        "Enable/reload the server, open an Agent chat, then send the Prompt tab text.",
      ],
    };
  }
  if (kind === "generic") {
    return {
      icon: Terminal,
      title: "Any MCP client can run the stdio bridge.",
      summary: "The generated config is plain stdio over docker exec. The client must support MCP tools/list and tools/call.",
      steps: [
        "Create a stdio MCP server entry named agvm-local-memory-os.",
        "Use the generated docker command/args so the bridge runs inside the AGVM API container.",
        "Do not add a local Python path unless you intentionally run a source checkout outside Docker.",
        "Connect the client, verify tools/list returns AGVM tools, then call get_agvm_usage_guide first.",
      ],
    };
  }
  return {
    icon: Plug,
    title: "Codex reads MCP servers from config.toml.",
    summary: "Open Codex Settings -> MCP servers or config.toml, replace the old AGVM block with the generated Docker TOML block, then start a new chat.",
    steps: [
      "In Codex, open Settings -> MCP servers. If needed, open Codex Settings -> Open config.toml.",
      "Delete the whole existing [mcp_servers.\"agvm-local-memory-os\"] block, including old env/cwd lines.",
      "Paste the generated Docker block from Client config. It should start with command=\"docker\" and have no cwd.",
      "For a real write-memory test, the block must include explicit_apply in AGVM_MCP_ALLOWED_PERMISSION_FAMILIES and must not include explicit_apply in AGVM_MCP_BLOCKED_PERMISSION_FAMILIES.",
      "Save config.toml, restart/reload Codex, then open a new Codex chat.",
      "Confirm agvm-local-memory-os is listed, then send the Prompt tab text.",
    ],
  };
}

function profileLabel(profile: PermissionProfile) {
  if (profile === "read_only_recall") return "Read-only recall";
  if (profile === "agent_onboarding") return "Agent onboarding";
  if (profile === "full_local_operator") return "Full operator";
  return "Preview-only learning";
}

function safetyProfileCopy(profile: PermissionProfile): { body: string; effects: string[]; title: string } {
  if (profile === "read_only_recall") {
    return {
      title: "Recall only",
      body: "The AI can retrieve context and inspect memory, but cannot create brains, learn new material or apply writes.",
      effects: ["Good for pure search tests.", "Not compatible with AI-created brains.", "Apply and destructive tools are hidden."],
    };
  }
  if (profile === "agent_onboarding") {
    return {
      title: "Onboarding only",
      body: "The AI can list, create or ensure brains, then retrieve context. Learning previews and apply tools stay blocked.",
      effects: ["Good for first connection tests.", "Allows ensure_brain for AI-created memory.", "Blocks preview learning and apply writes."],
    };
  }
  if (profile === "full_local_operator") {
    return {
      title: "Full local operator",
      body: "The AI can preview and apply writes. This is the mode required for write_memory_commit and real persistence tests.",
      effects: ["Use this for real write-memory tests.", "Apply calls need confirm_apply=true.", "Destructive operations remain blocked."],
    };
  }
  return {
    title: "Preview-only learning",
    body: "Recommended for testing: the AI can resolve/create brains and preview learning without committing memory writes.",
    effects: ["Can call ensure_brain.", "Can run grow/sleep/evolve preview tools.", "Cannot apply writes or destructive actions."],
  };
}

function brainPolicyTitle(policy: BrainPolicy) {
  if (policy === "fixed") return "Fixed brain";
  if (policy === "ai_resolve_existing") return "AI chooses an existing brain";
  return "AI creates or reuses a brain";
}

function brainScopeLabel(draft: McpSetupDraft, effectiveBrainId: string) {
  if (draft.runtimeScope === "hosted") return `Hosted: ${compactValue(`${draft.tenantId || "tenant"} / ${draft.userId || "user"}`)}`;
  if (draft.brainPolicy === "fixed") return `Fixed: ${compactValue(effectiveBrainId)}`;
  const displayName = draft.brainDisplayName.trim() || defaultDraft.brainDisplayName;
  if (draft.brainPolicy === "ai_resolve_existing") return `AI picks: ${compactValue(displayName)}`;
  return `AI creates: ${compactValue(displayName)}`;
}

function brainPolicyDescription(draft: McpSetupDraft, effectiveBrainId: string) {
  if (draft.brainPolicy === "fixed") {
    return `The bridge injects ${effectiveBrainId} when the AI omits brain_id. This is simplest for one known local brain.`;
  }
  if (draft.brainPolicy === "ai_resolve_existing") {
    return "The bridge does not inject a UI brain. The AI must choose an existing brain, then pass that brain_id on memory calls.";
  }
  return "The bridge does not inject a UI brain. The AI must call ensure_brain first and may create the target if it does not exist.";
}

function brainPolicyNextAction(policy: BrainPolicy) {
  if (policy === "fixed") return "The AI can call retrieve_context directly.";
  if (policy === "ai_resolve_existing") return "The AI must list or ensure an existing brain first.";
  return "The AI must ensure the brain first.";
}

function brainPolicyRuntimeNote(draft: McpSetupDraft, effectiveBrainId: string) {
  if (draft.brainPolicy === "fixed") return `Generated env includes AGVM_MCP_BRAIN_ID=${effectiveBrainId}.`;
  const displayName = draft.brainDisplayName.trim() || defaultDraft.brainDisplayName;
  const createFlag = draft.brainPolicy === "ai_create_if_missing" ? "true" : "false";
  const hint = draft.brainId.trim() ? ` with id hint ${draft.brainId.trim()}` : "";
  return `Generated env includes AGVM_MCP_BRAIN_POLICY=${draft.brainPolicy}; ensure_brain uses display name ${displayName}${hint} and create_if_missing=${createFlag}.`;
}

function brainPolicyChecklistDetail(draft: McpSetupDraft) {
  if (draft.runtimeScope === "hosted") return "Hosted scope resolves the brain server-side from tenant and user.";
  if (draft.brainPolicy === "fixed") return "A fixed local brain id is injected when the AI omits brain_id.";
  if (draft.brainPolicy === "ai_resolve_existing") return "The AI must resolve an existing brain before scoped memory calls.";
  return "The AI may create the target brain with ensure_brain before scoped memory calls.";
}

function isBrainStepReady(draft: McpSetupDraft, effectiveBrainId: string, profileConfig: ReturnType<typeof permissionProfileConfig>) {
  if (draft.runtimeScope === "hosted") return Boolean(draft.tenantId.trim() && draft.userId.trim());
  if (draft.brainPolicy === "fixed") return Boolean(effectiveBrainId);
  const registryWriteAllowed = profileConfig.allowed_permission_families.includes("registry_write") && !profileConfig.blocked_permission_families.includes("registry_write");
  return registryWriteAllowed && Boolean(draft.brainDisplayName.trim() || defaultDraft.brainDisplayName);
}

function isBrainPolicy(value: string | undefined): value is BrainPolicy {
  return value === "fixed" || value === "ai_resolve_existing" || value === "ai_create_if_missing";
}

function humanizeBrainId(value: string) {
  const text = String(value || "")
    .trim()
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ");
  if (!text) return "";
  return text
    .split(" ")
    .map((part) => (part ? `${part.slice(0, 1).toUpperCase()}${part.slice(1)}` : ""))
    .join(" ");
}

function defaultBrainDisplayName(activeBrainId: string) {
  return humanizeBrainId(activeBrainId) || defaultDraft.brainDisplayName;
}

function compactValue(value: string) {
  const text = String(value || "").trim();
  if (text.length <= 34) return text || "not set";
  return `${text.slice(0, 15)}...${text.slice(-14)}`;
}
