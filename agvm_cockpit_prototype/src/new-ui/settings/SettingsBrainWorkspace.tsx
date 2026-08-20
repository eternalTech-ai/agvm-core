import {
  Brain,
  CheckCircle2,
  Cloud,
  Code2,
  Database,
  FileText,
  FolderInput,
  HardDrive,
  KeyRound,
  Loader2,
  Lock,
  Plug,
  RefreshCw,
  Server,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  Terminal,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { API_BASE_URL } from "../../api/client";
import type { AgvmBrainSummary } from "../api/agvmClient";
import type { BrainResolutionId, BrainResolutionPreset } from "../brain/brainResolution";
import { SegmentedControl, type SegmentedControlOption } from "../components/SegmentedControl";
import type { OpsWorkspaceContext } from "../ops/opsWorkspaceTypes";
import { PLATFORM_BASE_URL } from "../platform/platformClient";
import { readCockpitShellProfile } from "../shell/coreShellPolicy";
import { ProductPageFrame } from "../shell/ProductPageFrame";

type CreateBrainForm = {
  brainId: string;
  description: string;
  displayName: string;
  makeActive: boolean;
  makeDefault: boolean;
};

type ImportBrainForm = {
  archivePath: string;
  brainId: string;
  displayName: string;
  makeActive: boolean;
  makeDefault: boolean;
  overwriteExisting: boolean;
};

type SettingsSectionId = "runtime" | "provider_keys" | "local_storage" | "mcp" | "cloud_account" | "advanced";

type SettingsSectionDefinition = {
  detail: string;
  icon: LucideIcon;
  id: SettingsSectionId;
  label: string;
  title: string;
};

const settingsSections: SettingsSectionDefinition[] = [
  {
    detail: "API URL, Docker services, UI profile and active brain scope.",
    icon: Server,
    id: "runtime",
    label: "Runtime",
    title: "Local runtime and brain scope",
  },
  {
    detail: "Provider secrets stay server-side and separate from Detwin login.",
    icon: KeyRound,
    id: "provider_keys",
    label: "Provider Keys",
    title: "Provider key storage",
  },
  {
    detail: "Browser drafts, session state and optional advanced import/export placement.",
    icon: FileText,
    id: "local_storage",
    label: "Local Storage",
    title: "Browser state and import tools",
  },
  {
    detail: "Client setup, safety profile, tool visibility and apply policy.",
    icon: Plug,
    id: "mcp",
    label: "MCP",
    title: "MCP setup and tool policy",
  },
  {
    detail: "Detwin account, hosted MCP, local access lease and optional cloud/local brain sync.",
    icon: Cloud,
    id: "cloud_account",
    label: "Detwin & Sync",
    title: "Detwin account, license and sync state",
  },
  {
    detail: "Support diagnostics, setup confirmations and guarded brain admin operations.",
    icon: SettingsIcon,
    id: "advanced",
    label: "Advanced",
    title: "Advanced diagnostics and guarded admin",
  },
];

export function SettingsBrainWorkspace({ context }: { context: OpsWorkspaceContext }) {
  const [activeSectionId, setActiveSectionId] = useState<SettingsSectionId>("runtime");
  const brainManagement = context.brainManagement;
  const renderSettings = context.renderSettings;
  const registry = brainManagement.registry;
  const activeBrain = brainManagement.activeBrain;
  const brains = useMemo(() => [...(registry?.brains || [])].sort(sortBrains(brainManagement.activeBrainId, registry?.default_brain_id || "")), [brainManagement.activeBrainId, registry]);
  const registryState = registryStatusLabel(context);
  const activeBrainLabel = activeBrain ? brainDisplayName(activeBrain) : brainManagement.activeBrainId || "No active brain";
  const graphDetail = graphResolutionLabel(renderSettings.presets, renderSettings.resolutionId);
  const activeSection = settingsSections.find((section) => section.id === activeSectionId) || settingsSections[0];

  useEffect(() => {
    const applyHashRoute = () => {
      const hash = window.location.hash.toLowerCase();
      if (!hash) return;
      if (hash.includes("provider")) setActiveSectionId("provider_keys");
      else if (hash.includes("mcp")) setActiveSectionId("mcp");
      else if (hash.includes("storage") || hash.includes("local-storage")) setActiveSectionId("local_storage");
      else if (hash.includes("advanced") || hash.includes("diagnostic")) setActiveSectionId("advanced");
      else if (hash.includes("brain") || hash.includes("sync") || hash.includes("brains-sync")) setActiveSectionId("runtime");
      else if (hash.includes("account") || hash.includes("license") || hash.includes("cloud")) setActiveSectionId("cloud_account");
      window.requestAnimationFrame(() => {
        const targetId = hash.replace(/^#/, "");
        if (!targetId) return;
        document.getElementById(targetId)?.scrollIntoView({ block: "start", behavior: "smooth" });
      });
    };
    applyHashRoute();
    window.addEventListener("hashchange", applyHashRoute);
    return () => window.removeEventListener("hashchange", applyHashRoute);
  }, []);

  return (
    <ProductPageFrame
      actions={[]}
      className="settings-brain-frame"
      eyebrow="Local cockpit"
      icon={SettingsIcon}
      intent="Task-specific local settings. Detwin account, local provider keys, MCP clients and brain sync are separate flows."
      metrics={[
        { label: "Runtime", value: context.health?.ok ? "connected" : context.health ? "reported" : "checking", detail: API_BASE_URL },
        { label: "Active brain", value: activeBrainLabel, detail: `${nodeCountLabel(activeBrain)} / ${safeForMcpLabel(activeBrain)}` },
        { label: "Provider key", value: "local service", detail: "not Detwin account key" },
        { label: "Graph detail", value: graphDetail, detail: renderSettings.graphLoading ? "loading active graph" : renderSettings.graphError || "ready" },
      ]}
      mode="settings"
      status={settingsStatus(context)}
      title="Cockpit Settings"
    >
      <section className="settings-brain-workspace settings-ux1-workspace">
        <SettingsControlCenter context={context} graphDetail={graphDetail} registryState={registryState} />
        {brainManagement.error ? <div className="settings-error">{brainManagement.error}</div> : null}
        <div className="settings-ux1-layout">
          <SettingsSectionNav activeSectionId={activeSectionId} context={context} onSectionChange={setActiveSectionId} registryState={registryState} />
          <section className={`settings-ux1-stage settings-ux1-stage-${activeSection.id}`} aria-label={activeSection.title}>
            <SettingsSectionHeader section={activeSection} status={settingsSectionStatus(activeSection.id, context, registryState)} />
            {renderSettingsSection(activeSection.id, context, brains, registryState)}
          </section>
        </div>
      </section>
    </ProductPageFrame>
  );
}

function SettingsControlCenter({
  context,
  graphDetail,
  registryState,
}: {
  context: OpsWorkspaceContext;
  graphDetail: string;
  registryState: string;
}) {
  const activeBrain = context.brainManagement.activeBrain;
  return (
    <section className="settings-ux1-control-center">
      <div className="settings-ux1-control-copy">
        <span>Settings map</span>
        <h2>Local runtime first. Detwin account only when you need cloud, license or sync.</h2>
        <p>Provider keys, MCP clients, active brain and Detwin account are separated so local setup never silently writes cloud settings.</p>
      </div>
      <div className="settings-ux1-control-ledger">
        <SettingsFact icon={Server} label="API URL" value={compactPath(API_BASE_URL)} />
        <SettingsFact icon={Cloud} label="Platform URL" value={compactPath(PLATFORM_BASE_URL)} />
        <SettingsFact icon={SettingsIcon} label="UI profile" value={readCockpitShellProfile()} />
        <SettingsFact icon={Brain} label="Active brain" value={activeBrain ? brainDisplayName(activeBrain) : context.brainManagement.activeBrainId || "none"} />
        <SettingsFact icon={Database} label="Registry" value={registryState} />
        <SettingsFact icon={HardDrive} label="Graph detail" value={graphDetail} />
      </div>
    </section>
  );
}

function SettingsSectionNav({
  activeSectionId,
  context,
  onSectionChange,
  registryState,
}: {
  activeSectionId: SettingsSectionId;
  context: OpsWorkspaceContext;
  onSectionChange: (sectionId: SettingsSectionId) => void;
  registryState: string;
}) {
  return (
    <nav className="settings-ux1-section-nav" aria-label="Cockpit settings sections">
      {settingsSections.map((section) => {
        const Icon = section.icon;
        const active = activeSectionId === section.id;
        return (
          <button
            aria-current={active ? "page" : undefined}
            className={active ? "active" : ""}
            key={section.id}
            onClick={() => onSectionChange(section.id)}
            type="button"
          >
            <Icon size={16} />
            <span>{section.label}</span>
            <strong>{settingsSectionStatus(section.id, context, registryState)}</strong>
            <small>{section.detail}</small>
          </button>
        );
      })}
    </nav>
  );
}

function SettingsSectionHeader({ section, status }: { section: SettingsSectionDefinition; status: string }) {
  const Icon = section.icon;
  return (
    <header className="settings-ux1-section-header">
      <Icon size={20} />
      <div>
        <span>{section.label}</span>
        <h2>{section.title}</h2>
        <p>{section.detail}</p>
      </div>
      <strong>{status}</strong>
    </header>
  );
}

function renderSettingsSection(
  sectionId: SettingsSectionId,
  context: OpsWorkspaceContext,
  brains: AgvmBrainSummary[],
  registryState: string,
) {
  if (sectionId === "runtime") {
    return (
      <div className="settings-ux1-section-body">
        <RuntimeSettingsPanel context={context} registryState={registryState} />
        <SettingsScopeHero context={context} registryState={registryState} />
        <div className="settings-ux1-runtime-grid">
          <GraphResolutionPanel context={context} />
          <BrainRegistryControl activeBrainId={context.brainManagement.activeBrainId} brains={brains} context={context} />
        </div>
        <BrainCommandPanel context={context} />
      </div>
    );
  }
  if (sectionId === "provider_keys") {
    return (
      <div className="settings-ux1-section-body">
        <ProviderKeysPanel />
      </div>
    );
  }
  if (sectionId === "local_storage") {
    return (
      <div className="settings-ux1-section-body">
        <LocalStoragePanel />
      </div>
    );
  }
  if (sectionId === "mcp") {
    return (
      <div className="settings-ux1-section-body">
        <McpSettingsPanel context={context} />
      </div>
    );
  }
  if (sectionId === "cloud_account") {
    return (
      <div className="settings-ux1-section-body">
        <CloudAccountSettingsPanel context={context} />
      </div>
    );
  }
  return (
    <div className="settings-ux1-section-body settings-ux1-advanced-body">
      <AdvancedSettingsPanel context={context} />
      <div className="settings-ux1-runtime-grid">
        <ScopeSafetyPanel context={context} />
        <LastOperationPanel context={context} />
      </div>
    </div>
  );
}

function SettingsScopeHero({ context, registryState }: { context: OpsWorkspaceContext; registryState: string }) {
  const registry = context.brainManagement.registry;
  const activeBrain = context.brainManagement.activeBrain;
  const activeBrainId = context.brainManagement.activeBrainId;
  return (
    <section className="settings-scope-hero">
      <div className="settings-scope-map" aria-hidden="true">
        <i />
        <i />
        <i />
        <span />
      </div>
      <div className="settings-scope-copy">
        <span>Active neural scope</span>
        <h2>{activeBrain ? brainDisplayName(activeBrain) : activeBrainId || "Registry loading"}</h2>
        <p>
          The cockpit runs against one active brain at a time. Selecting, creating or importing a brain refreshes the runtime scope and clears stale run overlays so Context,
          Results and operations stay tied to the correct memory graph.
        </p>
      </div>
      <div className="settings-scope-ledger">
        <SettingsFact icon={HardDrive} label="Brain root" value={compactPath(registry?.brain_root || "not reported")} />
        <SettingsFact icon={Server} label="Runtime" value={registry?.runtime_scope_status || context.health?.runtime_scope_status || "checking"} />
        <SettingsFact icon={Cloud} label="Cloud scope" value={context.health?.hosted_tenant_registry_ready ? "hosted ready" : "local self-hosted"} />
        <SettingsFact icon={ShieldCheck} label="MCP safety" value={safeForMcpLabel(activeBrain)} />
        <SettingsFact icon={Database} label="Registry" value={registryState} />
      </div>
    </section>
  );
}

function RuntimeSettingsPanel({ context, registryState }: { context: OpsWorkspaceContext; registryState: string }) {
  const registry = context.brainManagement.registry;
  return (
    <section className="settings-panel settings-runtime-panel">
      <PanelHeader icon={Server} label="Runtime" title="Brain Settings, local API and Docker truth" />
      <div className="settings-ux1-card-grid settings-ux1-card-grid-four">
        <SettingsInfoCard
          icon={Server}
          label="AGVM API"
          status={context.health?.ok ? "connected" : context.health ? "reported" : "checking"}
          detail={`Frontend calls ${API_BASE_URL}. Docker local API normally listens on 8010.`}
        />
        <SettingsInfoCard
          icon={Cloud}
          label="Detwin Platform"
          status={compactPath(PLATFORM_BASE_URL)}
          detail="The account console validates this URL. It may use the fallback port 8091 when 8090 is occupied."
        />
        <SettingsInfoCard
          icon={SettingsIcon}
          label="UI profile"
          status={readCockpitShellProfile()}
          detail="This profile controls which product surfaces are visible in the local cockpit."
        />
        <SettingsInfoCard
          icon={Database}
          label="Brain registry"
          status={registryState}
          detail={registry?.registry_path ? compactPath(String(registry.registry_path)) : "Registry path is reported by the local service when available."}
        />
      </div>
    </section>
  );
}

function ProviderKeysPanel() {
  return (
    <section className="settings-panel settings-provider-panel">
      <PanelHeader icon={KeyRound} label="Local Provider Key" title="Local model key stays in the local runtime" />
      <div className="settings-ux1-card-grid">
        <SettingsInfoCard
          icon={KeyRound}
          label="Local OpenAI API key"
          status="local backend / env"
          detail="This key powers the Docker/local AGVM runtime. It is configured through local MCP setup or backend env, not through Detwin cloud custody."
        />
        <SettingsInfoCard
          icon={Cloud}
          label="Detwin account key"
          status="separate"
          detail="Cloud AGVM provider keys are stored by Detwin for hosted runtime calls. They do not configure your local Docker backend."
        />
        <SettingsInfoCard
          icon={Plug}
          label="MCP bridge"
          status="no full provider key"
          detail="Generated MCP configs pass runtime URL and policy. The AI app should not receive the provider key through the bridge block."
        />
      </div>
      <SettingsActionStrip
        actions={[
          { icon: Plug, label: "Open local key setup", onClick: () => openLocalMode("mcp_setup"), reason: "Configure the local runtime key and MCP client without touching cloud custody." },
          { icon: Cloud, label: "Open Detwin Account", onClick: () => openLocalMode("platform"), reason: "Use only for login, billing, hosted MCP, licenses and optional sync." },
        ]}
      />
    </section>
  );
}

function LocalStoragePanel() {
  return (
    <section className="settings-panel settings-local-storage-panel">
      <PanelHeader icon={FileText} label="Local Storage" title="Browser state and advanced import tools" />
      <div className="settings-ux1-card-grid settings-ux1-card-grid-four">
        <SettingsInfoCard
          icon={FileText}
          label="MCP setup draft"
          status={mcpSetupDraftStatus()}
          detail="Only safe runtime/client fields are kept in localStorage. Raw provider keys are not stored here by this Settings page."
        />
        <SettingsInfoCard
          icon={Terminal}
          label="Session run state"
          status={sessionStorageStatus()}
          detail="Operation/session UI state can use browser sessionStorage so page switches do not lose the current operator context."
        />
        <SettingsInfoCard
          icon={Code2}
          label=".env import/export"
          status="advanced tool"
          detail="The .env import/export path belongs in MCP Setup advanced tools. It is not the normal account setup flow."
        />
        <SettingsInfoCard
          icon={Lock}
          label="Secrets"
          status="not persisted here"
          detail="Provider keys and future account tokens must stay in service/cloud secret stores or signed platform sessions."
        />
      </div>
      <SettingsActionStrip
        actions={[
          { icon: Plug, label: "Open MCP setup", onClick: () => openLocalMode("mcp_setup"), reason: "Use advanced import/export only when needed." },
        ]}
      />
    </section>
  );
}

function McpSettingsPanel({ context }: { context: OpsWorkspaceContext }) {
  const activeBrain = context.brainManagement.activeBrain;
  return (
    <section className="settings-panel settings-mcp-panel">
      <PanelHeader icon={Plug} label="MCP" title="Client setup, tool visibility and apply policy" />
      <div className="settings-ux1-card-grid">
        <SettingsInfoCard
          icon={Brain}
          label="Default brain policy"
          status={activeBrain ? brainDisplayName(activeBrain) : "AI-managed allowed"}
          detail="MCP clients can use a fixed brain or an AI-managed brain policy from the guided setup. The UI active brain can differ."
        />
        <SettingsInfoCard
          icon={ShieldCheck}
          label="Safety profile"
          status={safeForMcpLabel(activeBrain)}
          detail="Recall and preview are separate from explicit apply. Apply-capable tools still require the configured safety profile."
        />
        <SettingsInfoCard
          icon={Lock}
          label="Module tools"
          status="requires local access"
          detail="Generated local MCP configs hide module-required tools unless verified local module access exposes them."
        />
        <SettingsInfoCard
          icon={Code2}
          label="MCP diagnostics"
          status="advanced only"
          detail="Manual contract calls remain available for diagnostics, but the guided setup is the normal client connection path."
        />
      </div>
      <SettingsActionStrip
        actions={[
          { icon: Plug, label: "Open MCP setup", onClick: () => openLocalMode("mcp_setup"), reason: "Configure client, brain and safety policy." },
          { icon: Code2, label: "Open diagnostics", onClick: () => openLocalMode("mcp_raw_console"), reason: "Inspect low-level MCP contracts for support." },
        ]}
      />
    </section>
  );
}

function CloudAccountSettingsPanel({ context }: { context: OpsWorkspaceContext }) {
  return (
    <section className="settings-panel settings-cloud-account-panel">
      <PanelHeader icon={Cloud} label="Detwin & Sync" title="Account, paid access, hosted MCP and optional brain sync" />
      <div className="settings-ux1-card-grid settings-ux1-card-grid-four">
        <SettingsInfoCard
          icon={Cloud}
          label="Detwin account"
          status={context.health?.hosted_tenant_registry_ready ? "hosted registry ready" : "local account console"}
          detail={`Identity, billing, credits and paid module entitlement are shown through Detwin Account. Platform URL: ${PLATFORM_BASE_URL}.`}
        />
        <SettingsInfoCard
          icon={KeyRound}
          label="Hosted MCP access"
          status="account-owned"
          detail="Hosted MCP is for external AI clients using Detwin cloud endpoints. Local MCP is configured separately on this machine."
        />
        <SettingsInfoCard
          icon={ShieldCheck}
          label="Local access lease"
          status="explicit pairing"
          detail="Paid local modules require account verification and a local lease. Pairing does not automatically sync local brains."
        />
        <SettingsInfoCard
          icon={Brain}
          label="Brain sync"
          status="optional"
          detail="Cloud/local brain sync is explicit. Local-only brains remain local until upload/import receipts confirm the sync path."
        />
      </div>
      <SettingsActionStrip
        actions={[
          { icon: Cloud, label: "Open Detwin Account", onClick: () => openLocalMode("platform"), reason: "View account, billing, licenses, hosted MCP and optional brain sync." },
          { icon: Plug, label: "Open local MCP setup", onClick: () => openLocalMode("mcp_setup"), reason: "Configure the local bridge without touching hosted MCP keys." },
        ]}
      />
    </section>
  );
}

function AdvancedSettingsPanel({ context }: { context: OpsWorkspaceContext }) {
  const registry = context.brainManagement.registry;
  return (
    <section className="settings-panel settings-advanced-panel">
      <PanelHeader icon={SettingsIcon} label="Advanced" title="Diagnostics, setup confirmations and guarded admin operations" />
      <div className="settings-ux1-card-grid">
        <SettingsInfoCard
          icon={Code2}
          label="MCP diagnostics"
          status="manual contracts"
          detail="Contract calls are available for diagnostics. Mutating calls must still follow preview and explicit apply policy."
        />
        <SettingsInfoCard
          icon={CheckCircle2}
          label="Last setup confirmation"
          status={context.brainManagement.lastOperation?.status || "none"}
          detail="Create/import/select operations report confirmations here without exposing delete/reset controls."
        />
        <SettingsInfoCard
          icon={Database}
          label="Registry JSON"
          status={registry?.schema_version || "service reported"}
          detail="Registry metadata remains service-owned. This panel only exposes safe summaries and operation confirmations."
        />
      </div>
      <SettingsActionStrip
        actions={[
          { icon: Code2, label: "Open diagnostics", onClick: () => openLocalMode("mcp_raw_console"), reason: "Use only for diagnostics and contract-level checks." },
        ]}
      />
    </section>
  );
}

function BrainRegistryControl({
  activeBrainId,
  brains,
  context,
}: {
  activeBrainId: string;
  brains: AgvmBrainSummary[];
  context: OpsWorkspaceContext;
}) {
  return (
    <section className="settings-panel settings-registry-panel">
      <PanelHeader icon={Brain} label="Brain Registry Control" title="Select the active memory graph" />
      <button className="settings-inline-action" disabled={context.brainManagement.busyAction === "refresh"} onClick={() => void context.brainManagement.onRefreshRegistry()} type="button">
        {context.brainManagement.busyAction === "refresh" ? <Loader2 size={14} /> : <RefreshCw size={14} />}
        <span>{context.brainManagement.busyAction === "refresh" ? "Refreshing registry" : "Refresh registry"}</span>
        <small>Reload /memory/brains and health scope without changing memory.</small>
      </button>
      <div className="settings-registry-list">
        {brains.length ? (
          brains.map((brain) => {
            const brainId = brainIdOf(brain);
            const active = Boolean(brainId && brainId === activeBrainId);
            return (
              <article className={active ? "settings-brain-row active" : "settings-brain-row"} key={brainId || brainDisplayName(brain)}>
                <div className="settings-brain-row-main">
                  <strong>{brainDisplayName(brain)}</strong>
                  <span>{brainId || "no id"} / {nodeCountLabel(brain)} / {safeForMcpLabel(brain)}</span>
                  <small>{compactPath(String(brain.registry_brain_path || brain.storage_path || "storage path not reported"))}</small>
                </div>
                <div className="settings-brain-tags">
                  {active ? <em>active</em> : null}
                  {brain.is_default ? <em>default</em> : null}
                  {brain.migration_status ? <small title={brain.migration_status}>{brain.migration_status}</small> : null}
                </div>
                <button disabled={active || !brainId || context.brainManagement.switching} onClick={() => void context.brainManagement.onSelectBrain(brainId)} type="button">
                  {context.brainManagement.switching && !active ? <Loader2 size={13} /> : <CheckCircle2 size={13} />}
                  <span>{active ? "Active brain" : "Activate brain"}</span>
                </button>
              </article>
            );
          })
        ) : (
          <div className="settings-empty-state">No registry entries are loaded. Refresh the registry or create a brain.</div>
        )}
      </div>
    </section>
  );
}

function BrainCommandPanel({ context }: { context: OpsWorkspaceContext }) {
  const [createForm, setCreateForm] = useState<CreateBrainForm>({
    brainId: "",
    description: "",
    displayName: "",
    makeActive: true,
    makeDefault: false,
  });
  const [importForm, setImportForm] = useState<ImportBrainForm>({
    archivePath: "",
    brainId: "",
    displayName: "",
    makeActive: true,
    makeDefault: false,
    overwriteExisting: false,
  });
  const busy = context.brainManagement.busyAction;

  const runCreate = async () => {
    await context.brainManagement.onCreateBrain({
      brain_id: createForm.brainId,
      description: createForm.description,
      display_name: createForm.displayName,
      make_active: createForm.makeActive,
      make_default: createForm.makeDefault,
    });
  };
  const runImport = async () => {
    await context.brainManagement.onImportBrainArchive({
      archive_path: importForm.archivePath,
      brain_id: importForm.brainId,
      display_name: importForm.displayName,
      make_active: importForm.makeActive,
      make_default: importForm.makeDefault,
      overwrite_existing: importForm.overwriteExisting,
    });
  };

  return (
    <section className="settings-panel settings-command-panel" id="brains-sync">
      <PanelHeader icon={Sparkles} label="Brain lifecycle" title="Create or import a local brain" />
      <div className="settings-command-grid">
        <article className="settings-command-card">
          <div className="settings-command-head">
            <Brain size={17} />
            <div>
              <span>Create brain</span>
              <strong>Empty local brain</strong>
            </div>
          </div>
          <div className="settings-form-grid">
            <label>
              <span>Display name</span>
              <input value={createForm.displayName} onChange={(event) => setCreateForm((current) => ({ ...current, displayName: event.target.value }))} placeholder="Project or person name" />
            </label>
            <label>
              <span>Brain id</span>
              <input value={createForm.brainId} onChange={(event) => setCreateForm((current) => ({ ...current, brainId: event.target.value }))} placeholder="optional_stable_id" />
            </label>
            <label className="settings-wide-field">
              <span>Description</span>
              <textarea value={createForm.description} onChange={(event) => setCreateForm((current) => ({ ...current, description: event.target.value }))} placeholder="optional human scope" rows={3} />
            </label>
          </div>
          <SettingsCheck
            checked={createForm.makeActive}
            label="Activate after create"
            onChange={(value) => setCreateForm((current) => ({ ...current, makeActive: value }))}
          />
          <SettingsCheck
            checked={createForm.makeDefault}
            label="Make default brain"
            onChange={(value) => setCreateForm((current) => ({ ...current, makeDefault: value }))}
          />
          <button className="settings-primary-action" disabled={!createForm.displayName.trim() || Boolean(busy)} onClick={() => void runCreate()} type="button">
            {busy === "create" ? <Loader2 size={14} /> : <Sparkles size={14} />}
            <span>{busy === "create" ? "Creating brain" : "Create brain"}</span>
          </button>
        </article>

        <article className="settings-command-card">
          <div className="settings-command-head">
            <FolderInput size={17} />
            <div>
              <span>Import archive</span>
              <strong>Server-local export</strong>
            </div>
          </div>
          <div className="settings-form-grid">
            <label className="settings-wide-field">
              <span>Archive path</span>
              <input value={importForm.archivePath} onChange={(event) => setImportForm((current) => ({ ...current, archivePath: event.target.value }))} placeholder="C:\\path\\brain_export.zip" />
            </label>
            <label>
              <span>Brain id</span>
              <input value={importForm.brainId} onChange={(event) => setImportForm((current) => ({ ...current, brainId: event.target.value }))} placeholder="optional target id" />
            </label>
            <label>
              <span>Display name</span>
              <input value={importForm.displayName} onChange={(event) => setImportForm((current) => ({ ...current, displayName: event.target.value }))} placeholder="optional label" />
            </label>
          </div>
          <SettingsCheck
            checked={importForm.makeActive}
            label="Activate imported brain"
            onChange={(value) => setImportForm((current) => ({ ...current, makeActive: value }))}
          />
          <SettingsCheck
            checked={importForm.makeDefault}
            label="Make imported brain default"
            onChange={(value) => setImportForm((current) => ({ ...current, makeDefault: value }))}
          />
          <SettingsCheck
            checked={importForm.overwriteExisting}
            label="Overwrite existing id"
            onChange={(value) => setImportForm((current) => ({ ...current, overwriteExisting: value }))}
          />
          <p className="settings-command-note">Import reads an archive already present on this machine. Cloud sync remains explicit through Detwin Account receipts.</p>
          <button className="settings-primary-action" disabled={!importForm.archivePath.trim() || Boolean(busy)} onClick={() => void runImport()} type="button">
            {busy === "import" ? <Loader2 size={14} /> : <FolderInput size={14} />}
            <span>{busy === "import" ? "Importing archive" : "Import archive"}</span>
          </button>
        </article>
      </div>
    </section>
  );
}

function GraphResolutionPanel({ context }: { context: OpsWorkspaceContext }) {
  const settings = context.renderSettings;
  const resolutionOptions = useMemo<SegmentedControlOption<BrainResolutionId>[]>(
    () =>
      settings.presets.map((preset) => ({
        disabled: settings.graphLoading && preset.id === settings.resolutionId,
        label: preset.label,
        meta: resolutionLimitLabel(preset),
        title: preset.description,
        value: preset.id,
      })),
    [settings.graphLoading, settings.presets, settings.resolutionId],
  );
  return (
    <section className="settings-panel settings-resolution-panel">
      <PanelHeader icon={Database} label="Graph resolution" title="How much of the active brain is rendered" />
      <SegmentedControl
        className="settings-resolution-control"
        label="Render detail"
        onChange={settings.onResolutionChange}
        options={resolutionOptions}
        value={settings.resolutionId}
      />
      <div className="settings-resolution-receipt">
        <SettingsFact icon={Brain} label="Visible graph" value={`${context.graphNodeCount.toLocaleString()} nodes`} />
        <SettingsFact icon={Database} label="Total graph" value={`${context.totalNodeCount.toLocaleString()} nodes`} />
        <SettingsFact icon={RefreshCw} label="Load state" value={settings.graphLoading ? "loading" : settings.graphError || "ready"} />
      </div>
      <button className="settings-inline-action" disabled={settings.graphLoading} onClick={context.onRefreshGraph} type="button">
        {settings.graphLoading ? <Loader2 size={14} /> : <Database size={14} />}
        <span>{settings.graphLoading ? "Graph loading" : "Reload graph"}</span>
        <small>Reload the active brain graph using the selected resolution.</small>
      </button>
    </section>
  );
}

function ScopeSafetyPanel({ context }: { context: OpsWorkspaceContext }) {
  const registry = context.brainManagement.registry;
  const productBoundary = registry?.product_boundary || {};
  const validation = registry?.validation || {};
  return (
    <section className="settings-panel settings-safety-panel">
      <PanelHeader icon={ShieldCheck} label="Scope and safety" title="What this cockpit may do" />
      <div className="settings-safety-grid">
        <SettingsFact icon={HardDrive} label="Runtime boundary" value={String(productBoundary.runtime_boundary || registry?.runtime_scope_status || "local brain scoped")} />
        <SettingsFact icon={Cloud} label="Hosted registry" value={context.health?.hosted_tenant_registry_ready ? "ready" : "not active"} />
        <SettingsFact icon={Lock} label="Mutations" value="guarded previews only" />
        <SettingsFact icon={CheckCircle2} label="Validation" value={registryValidationLabel(validation)} />
      </div>
      <div className="settings-safety-callouts">
        <p>
          <Lock size={14} />
          <strong>Delete is intentionally unavailable.</strong>
          <span>Dangerous brain deletion exists only in protected admin contracts and is not exposed in this operator page.</span>
        </p>
        <p>
          <ShieldCheck size={14} />
          <strong>MCP diagnostics remain separate.</strong>
          <span>Manual low-level tool checks stay outside this product cockpit until separately certified.</span>
        </p>
      </div>
    </section>
  );
}

function LastOperationPanel({ context }: { context: OpsWorkspaceContext }) {
  const operation = context.brainManagement.lastOperation;
  return (
    <section className="settings-panel settings-operation-panel">
      <PanelHeader icon={CheckCircle2} label="Operation confirmation" title={operation ? `${operation.action || "operation"} / ${operation.status || "reported"}` : "No admin operation yet"} />
      {operation ? (
        <div className="settings-operation-receipt">
          <SettingsFact icon={Brain} label="Brain" value={operation.brain_id || brainIdOf(operation.brain || {}) || "not reported"} />
          <SettingsFact icon={Database} label="Registry count" value={operation.registry?.brain_count ? `${operation.registry.brain_count} brains` : "not reported"} />
          <SettingsFact icon={FolderInput} label="Archive" value={compactPath(operation.archive_path || "none")} />
          <SettingsFact icon={ShieldCheck} label="Warnings" value={operation.warnings?.length ? operation.warnings.join(", ") : "none"} />
        </div>
      ) : (
        <p className="settings-empty-state">Create, import or select a brain to see the last admin confirmation here.</p>
      )}
    </section>
  );
}

type SettingsAction = {
  icon: LucideIcon;
  label: string;
  onClick: () => void;
  reason: string;
};

function SettingsInfoCard({
  detail,
  icon: Icon,
  label,
  status,
}: {
  detail: string;
  icon: LucideIcon;
  label: string;
  status: string;
}) {
  return (
    <article className="settings-ux1-info-card">
      <div className="settings-ux1-info-card-head">
        <Icon size={15} />
        <span>{label}</span>
      </div>
      <strong title={status}>{status}</strong>
      <p>{detail}</p>
    </article>
  );
}

function SettingsActionStrip({ actions }: { actions: SettingsAction[] }) {
  return (
    <div className="settings-ux1-action-strip">
      {actions.map((action) => {
        const Icon = action.icon;
        return (
          <button key={action.label} onClick={action.onClick} title={action.reason} type="button">
            <Icon size={15} />
            <span>{action.label}</span>
            <small>{action.reason}</small>
          </button>
        );
      })}
    </div>
  );
}

function PanelHeader({ icon: Icon, label, title }: { icon: LucideIcon; label: string; title: string }) {
  return (
    <header className="settings-panel-head">
      <Icon size={17} />
      <div>
        <span>{label}</span>
        <strong>{title}</strong>
      </div>
    </header>
  );
}

function SettingsFact({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <div className="settings-fact">
      <Icon size={14} />
      <span>{label}</span>
      <strong title={value}>{value}</strong>
    </div>
  );
}

function SettingsCheck({ checked, label, onChange }: { checked: boolean; label: string; onChange: (value: boolean) => void }) {
  return (
    <label className="settings-check">
      <input checked={checked} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
      <span>{label}</span>
    </label>
  );
}

function sortBrains(activeBrainId: string, defaultBrainId: string) {
  return (first: AgvmBrainSummary, second: AgvmBrainSummary) => {
    const firstId = brainIdOf(first);
    const secondId = brainIdOf(second);
    if (firstId === activeBrainId) return -1;
    if (secondId === activeBrainId) return 1;
    if (firstId === defaultBrainId) return -1;
    if (secondId === defaultBrainId) return 1;
    return brainDisplayName(first).localeCompare(brainDisplayName(second));
  };
}

function brainIdOf(brain: AgvmBrainSummary | Record<string, unknown>) {
  return String(brain.brain_id || brain.id || "").trim();
}

function brainDisplayName(brain: AgvmBrainSummary) {
  const id = brainIdOf(brain);
  return String(brain.display_name || brain.name || id || "Unnamed brain").trim();
}

function nodeCountLabel(brain: AgvmBrainSummary | null) {
  if (!brain || typeof brain.node_count !== "number") return "nodes unknown";
  return `${brain.node_count.toLocaleString()} nodes`;
}

function safeForMcpLabel(brain: AgvmBrainSummary | null) {
  if (!brain) return "safety unknown";
  if (brain.safe_for_mcp === true) return "MCP ready";
  if (brain.safe_for_mcp === false) return "MCP not ready";
  return "MCP safety unreported";
}

function settingsStatus(context: OpsWorkspaceContext) {
  if (context.brainManagement.busyAction) return `${context.brainManagement.busyAction} running`;
  if (context.brainManagement.switching) return "switching active brain";
  if (context.brainManagement.error) return "attention required";
  if (context.brainManagement.status === "ready") return "registry ready";
  return context.brainManagement.status;
}

function settingsSectionStatus(sectionId: SettingsSectionId, context: OpsWorkspaceContext, registryState: string) {
  if (sectionId === "runtime") return context.health?.ok ? "connected" : registryState;
  if (sectionId === "provider_keys") return "server-side";
  if (sectionId === "local_storage") return localStorageStatus();
  if (sectionId === "mcp") return safeForMcpLabel(context.brainManagement.activeBrain);
  if (sectionId === "cloud_account") return context.health?.hosted_tenant_registry_ready ? "hosted ready" : "local only";
  return context.brainManagement.lastOperation?.status || "manual only";
}

function localStorageStatus() {
  try {
    if (typeof window === "undefined" || !window.localStorage) return "not available";
    return "available";
  } catch {
    return "blocked";
  }
}

function sessionStorageStatus() {
  try {
    if (typeof window === "undefined" || !window.sessionStorage) return "not available";
    return "available";
  } catch {
    return "blocked";
  }
}

function mcpSetupDraftStatus() {
  try {
    if (typeof window === "undefined" || !window.localStorage) return "not available";
    return window.localStorage.getItem("agvm.mcpSetupDraft.v2") ? "draft saved" : "no draft";
  } catch {
    return "blocked";
  }
}

function openLocalMode(mode: string) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set("mode", mode);
  url.searchParams.delete("route");
  window.location.href = url.toString();
}

function registryStatusLabel(context: OpsWorkspaceContext) {
  const registry = context.brainManagement.registry;
  if (context.brainManagement.status === "loading") return "loading";
  if (!registry) return context.brainManagement.status;
  const count = registry.brain_count ?? registry.brains?.length ?? 0;
  return `${count} brains / ${registry.runtime_scope_status || "runtime scoped"}`;
}

function graphResolutionLabel(presets: BrainResolutionPreset[], resolutionId: BrainResolutionId) {
  const preset = presets.find((candidate) => candidate.id === resolutionId);
  return preset ? preset.label : resolutionId;
}

function resolutionLimitLabel(preset: BrainResolutionPreset) {
  return preset.maxGraphNodes === "full" ? "full graph request" : `${preset.maxGraphNodes.toLocaleString()} max nodes`;
}

function registryValidationLabel(validation: Record<string, unknown>) {
  const values = Object.entries(validation);
  if (!values.length) return "not reported";
  const failed = values.filter(([, value]) => value === false).map(([key]) => key);
  if (failed.length) return `attention: ${failed.slice(0, 2).join(", ")}`;
  return "checks green";
}

function compactPath(value: string) {
  const text = String(value || "").trim();
  if (!text) return "not reported";
  if (text.length <= 52) return text;
  return `${text.slice(0, 21)}...${text.slice(-24)}`;
}
