import { useMemo, useState } from "react";

import type { BrainLayerState } from "../brain/brainLayers";
import { Brain3DInspector } from "../brain/Brain3DInspector";
import type { BrainResolutionId, BrainResolutionPreset } from "../brain/brainResolution";
import type { BrainSceneModel } from "../brain/brainSceneModel";
import type { BrainVisualModel } from "../brain/brainVisualModel";
import type { BrainOpsOverlay } from "../contracts/opsOverlayContracts";
import type { MissionProjection } from "../mission/missionProjection";
import { ContextLivePanel } from "./ContextLivePanel";
import { MissionCommandBar, MissionSettingsPanel, type MissionComposerProps, type MissionComposerViewModel } from "./MissionComposer";

type BrainStageProps = {
  activeBrainId?: string | null;
  model: BrainSceneModel;
  visualModel: BrainVisualModel;
  resolution: BrainResolutionPreset;
  loading: boolean;
  error: string | null;
  mission: MissionProjection | null;
  reasoningActive: boolean;
  opsOverlay?: BrainOpsOverlay | null;
  clientView: boolean;
  layerState: BrainLayerState;
  selectedNodeId?: string | null;
  composer: MissionComposerViewModel;
  onOpen3d: () => void;
  onResolutionChange: (resolution: BrainResolutionId) => void;
  onClientViewChange: (enabled: boolean) => void;
  onLayerStateChange: (state: BrainLayerState) => void;
  onInspectNode: (nodeId: string | null) => void;
  onComposerQueryChange: (value: string) => void;
  onComposerToolChange: MissionComposerProps["onToolChange"];
  onComposerModeChange: MissionComposerProps["onModeChange"];
  onComposerRefsPolicyChange: MissionComposerProps["onRefsPolicyChange"];
  onComposerCompletePathsChange: MissionComposerProps["onCompletePathsChange"];
  onComposerIncludeAnswerDemoChange: MissionComposerProps["onIncludeAnswerDemoChange"];
  onCorrectionApplied?: () => void;
  onOpenResults: () => void;
  onRunMission: () => void;
};

export function BrainStage({
  activeBrainId,
  model,
  visualModel,
  resolution,
  loading,
  error,
  mission,
  reasoningActive,
  opsOverlay,
  clientView,
  layerState,
  selectedNodeId,
  composer,
  onOpen3d,
  onResolutionChange,
  onClientViewChange,
  onLayerStateChange,
  onInspectNode,
  onComposerQueryChange,
  onComposerToolChange,
  onComposerModeChange,
  onComposerRefsPolicyChange,
  onComposerCompletePathsChange,
  onComposerIncludeAnswerDemoChange,
  onCorrectionApplied,
  onOpenResults,
  onRunMission,
}: BrainStageProps) {
  void visualModel;
  void onClientViewChange;

  const [railFocusNodeIds, setRailFocusNodeIds] = useState<string[]>([]);
  const focusedNodeIds = useMemo(
    () => [...new Set([selectedNodeId || "", ...railFocusNodeIds].map((id) => id.trim()).filter(Boolean))],
    [railFocusNodeIds, selectedNodeId],
  );
  const settingsSlot = (
    <MissionSettingsPanel
      onCompletePathsChange={onComposerCompletePathsChange}
      onIncludeAnswerDemoChange={onComposerIncludeAnswerDemoChange}
      onModeChange={onComposerModeChange}
      onRefsPolicyChange={onComposerRefsPolicyChange}
      onToolChange={onComposerToolChange}
      view={composer}
    />
  );

  return (
    <section className="et-stage brain-stage-with-composer">
      <MissionCommandBar onQueryTextChange={onComposerQueryChange} onRun={onRunMission} view={composer} />
      <div className="context-live-layout">
        <div className="brain-canvas-zone">
          <Brain3DInspector
            clientView={clientView}
            focusNodeIds={focusedNodeIds}
            layerState={layerState}
            loading={loading}
            mission={mission}
            opsOverlay={opsOverlay}
            reasoningActive={reasoningActive}
            model={model}
            onLayerStateChange={onLayerStateChange}
            onResolutionChange={onResolutionChange}
            onOpenFocus={onOpen3d}
            resolution={resolution}
            showControls={false}
            showTopbar={false}
            variant="inline"
          />
          <div className="context-brain-settings-overlay">
            {settingsSlot}
          </div>
          <div className={`context-brain-live ${composer.running ? "running" : composer.activeMission ? "ready" : "idle"}`}>
            <i />
            <div>
              <span>{composer.running ? "Reading memory" : composer.activeMission ? "Latest context" : "Ready"}</span>
              <strong>{composer.running ? composer.liveEventLabel || composer.activeMission?.payloadState || "Starting" : composer.activeMission?.clientState || "Awaiting request"}</strong>
              <small>{composer.running ? composer.liveEventSummary || `${composer.liveEventCount} live updates` : composer.activeMission?.id || "No active search"}</small>
            </div>
          </div>
          {error ? <div className="brain-error">Graph unavailable: {error}</div> : null}
        </div>
        <ContextLivePanel
          activeBrainId={activeBrainId}
          composer={composer}
          focusedNodeIds={focusedNodeIds}
          mission={mission}
          model={model}
          onClearFocus={() => {
            setRailFocusNodeIds([]);
            onInspectNode(null);
          }}
          onFocusNodeIds={(nodeIds) => {
            setRailFocusNodeIds(nodeIds);
            onInspectNode(nodeIds[0] || null);
          }}
          onCorrectionApplied={onCorrectionApplied}
          onOpenResults={onOpenResults}
        />
      </div>
    </section>
  );
}
