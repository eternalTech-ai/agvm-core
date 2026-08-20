import type { CockpitModeKey } from "../shell/ModeRail";

export type OpenCoreSurfaceCategory =
  | "core"
  | "paid_module"
  | "platform_only"
  | "dev_only"
  | "deprecated"
  | "compat";

export type CockpitModeClassification = {
  mode: CockpitModeKey;
  category: OpenCoreSurfaceCategory;
  owner: string;
  publicCoreAllowed: boolean;
  rationale: string;
};

export const cockpitModeClassifications = [
  {
    mode: "brain",
    category: "core",
    owner: "agvm_core_graph_viewer",
    publicCoreAllowed: true,
    rationale: "Base 3D brain viewer stays in public core.",
  },
  {
    mode: "retrieve",
    category: "core",
    owner: "agvm_core_retrieve",
    publicCoreAllowed: true,
    rationale: "Context retrieval is the primary open-core workflow.",
  },
  {
    mode: "payload",
    category: "core",
    owner: "agvm_core_retrieve",
    publicCoreAllowed: true,
    rationale: "Results and proof view are part of retrieve proof.",
  },
  {
    mode: "paths",
    category: "core",
    owner: "agvm_core_retrieve",
    publicCoreAllowed: true,
    rationale: "Legacy route proof alias for results.",
  },
  {
    mode: "documents",
    category: "core",
    owner: "agvm_core_retrieve",
    publicCoreAllowed: true,
    rationale: "Legacy document proof alias for results.",
  },
  {
    mode: "health",
    category: "core",
    owner: "agvm_core_health",
    publicCoreAllowed: true,
    rationale: "Brain health is a public proof and safety surface.",
  },
  {
    mode: "benchmarks",
    category: "core",
    owner: "agvm_core_bench",
    publicCoreAllowed: true,
    rationale: "Base benchmark surface remains core.",
  },
  {
    mode: "mcp_setup",
    category: "core",
    owner: "agvm_core_mcp",
    publicCoreAllowed: true,
    rationale: "MCP setup is required for local open-core adoption.",
  },
  {
    mode: "mcp_raw_console",
    category: "core",
    owner: "agvm_core_mcp_raw_console",
    publicCoreAllowed: true,
    rationale: "Raw MCP contract console lets public-core users call core MCP tools without paid module UI.",
  },
  {
    mode: "settings",
    category: "core",
    owner: "agvm_core_settings",
    publicCoreAllowed: true,
    rationale: "Minimal local settings stay in core.",
  },
  {
    mode: "platform",
    category: "platform_only",
    owner: "agvm_platform",
    publicCoreAllowed: false,
    rationale: "Central account, billing, entitlement and cloud control-plane surfaces are private platform code.",
  },
  {
    mode: "clone_app",
    category: "paid_module",
    owner: "agvm_clone_app",
    publicCoreAllowed: false,
    rationale: "Clone App is a paid module.",
  },
  {
    mode: "chat",
    category: "paid_module",
    owner: "agvm_agent_chat",
    publicCoreAllowed: false,
    rationale: "Non-core assistant chat should become an optional module.",
  },
  {
    mode: "grow",
    category: "paid_module",
    owner: "agvm_grow_studio",
    publicCoreAllowed: false,
    rationale: "Grow Studio rich source workflow is Pro.",
  },
  {
    mode: "evolve",
    category: "paid_module",
    owner: "agvm_maintain_studio",
    publicCoreAllowed: false,
    rationale: "Maintain, Sleep and Evolve rich workflow is Pro.",
  },
] as const satisfies readonly CockpitModeClassification[];

export const cockpitModeClassificationByMode: Record<CockpitModeKey, CockpitModeClassification> =
  Object.fromEntries(cockpitModeClassifications.map((item) => [item.mode, item])) as Record<
    CockpitModeKey,
    CockpitModeClassification
  >;

export function classifyCockpitMode(mode: CockpitModeKey): CockpitModeClassification {
  return cockpitModeClassificationByMode[mode];
}

export function isPublicCoreCockpitMode(mode: CockpitModeKey): boolean {
  return classifyCockpitMode(mode).publicCoreAllowed;
}
