import { Orbit } from "lucide-react";

import { Brain3DInspector } from "../brain/Brain3DInspector";
import { defaultBrainLayerState } from "../brain/brainLayers";
import { brainOpsRenderPlanRows, createBrainOpsOverlayRenderPlan } from "../brain/brainOpsOverlayRenderPlan";
import type { BrainOpsOverlay } from "../contracts/opsOverlayContracts";
import { DefinitionRows } from "./BrainFirstWorkspace";
import type { OpsWorkspaceContext } from "./opsWorkspaceTypes";

export function OpsBrainPreview({ context, overlay }: { context: OpsWorkspaceContext; overlay: BrainOpsOverlay }) {
  const renderPlan = createBrainOpsOverlayRenderPlan(overlay, context.model);
  return (
    <>
      <div className={`ops-animation-truth ops-animation-truth-${renderPlan.state}`}>
        <span>Animation truth gate</span>
        <strong>{renderPlan.label}</strong>
        <em>{renderPlan.detail}</em>
      </div>
      <div className="ops-brain-preview">
        <Brain3DInspector
          layerState={defaultBrainLayerState}
          loading={false}
          model={context.model}
          opsOverlay={overlay}
          resolution={context.model.resolution}
          showControls={false}
          variant="inline"
        />
      </div>
      <DefinitionRows rows={brainOpsRenderPlanRows(renderPlan)} />
    </>
  );
}

export const opsBrainPreviewIcon = Orbit;
