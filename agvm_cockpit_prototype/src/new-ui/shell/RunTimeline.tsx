import type { MissionProjection } from "../mission/missionProjection";

type TimelineState = "done" | "pending";
type TimelineStep = [label: string, time: string, state: TimelineState];

type RunTimelineProps = {
  liveRun?: {
    eventCount: number;
    latestEventLabel: string;
    latestEventSummary: string;
    running: boolean;
  };
  mission: MissionProjection | null;
};

export function RunTimeline({ liveRun, mission }: RunTimelineProps) {
  const steps = missionTimeline(mission);
  const progress = timelineProgress(mission, liveRun);
  const live = Boolean(liveRun?.running || mission?.status === "running");
  return (
    <footer className={mission ? `et-timeline active-run ${live ? "live-run" : "settled-run"}` : "et-timeline idle-run"} aria-label="Run pipeline">
      <div className="timeline-head">
        <span>Run pipeline</span>
        <strong>{timelineTitle(mission, liveRun)}</strong>
      </div>
      <div className="timeline-progress" aria-label={`Run progress ${progress}%`}>
        <span style={{ width: `${progress}%` }} />
      </div>
      <div className="timeline-track">
        {steps.map(([label, time, state]) => (
          <div className={`time-step ${state}`} key={label}>
            <i />
            <div>
              <strong>{label}</strong>
              <span>{time}</span>
            </div>
          </div>
        ))}
      </div>
    </footer>
  );
}

function timelineTitle(mission: MissionProjection | null, liveRun?: RunTimelineProps["liveRun"]) {
  if (!mission) return "Idle";
  if (liveRun?.running) return liveRun.latestEventLabel || "Live MCP running";
  if (mission.live?.terminalForClient) return "Terminal context";
  return mission.source === "live_mcp" ? mission.summary.clientCanProceed || "Live MCP" : "Fixture projection";
}

function timelineProgress(mission: MissionProjection | null, liveRun?: RunTimelineProps["liveRun"]) {
  if (!mission) return 0;
  if (mission.live?.terminalForClient) return 100;
  const landingCount = mission.landingNodeIds.length || mission.projection.anchors.filter((anchor) => anchor.role === "landing").length;
  const eventProgress = Math.min(26, Math.max(0, liveRun?.eventCount || 0) * 3);
  const materialProgress =
    Math.min(20, landingCount * 8) +
    Math.min(16, mission.routeNodeIds.length * 2) +
    Math.min(16, mission.documentRefs.length * 5) +
    Math.min(12, mission.payloadNodeIds.length * 2);
  if (liveRun?.running || mission.status === "running") return Math.max(12, Math.min(88, 18 + eventProgress + materialProgress));
  return Math.max(72, Math.min(96, 56 + materialProgress));
}

function missionTimeline(mission: MissionProjection | null): TimelineStep[] {
  if (!mission) {
    return [
      ["Request", "idle", "pending"],
      ["Brain", "standby", "pending"],
      ["Context", "waiting", "pending"],
      ["Documents", "waiting", "pending"],
      ["Delivery", "none", "pending"],
    ];
  }
  const isLive = mission.source === "live_mcp";

  return [
    ["Request", "captured", "done"],
    ["Brain", isLive ? "backend context" : "fixture projection", "done"],
    ["Semantic Contract", mission.summary.semanticAi, "done"],
    ["Spatial Landing", `${mission.landingNodeIds.length} landings`, mission.landingNodeIds.length ? "done" : "pending"],
    ["Branch Route", mission.summary.pathTruth, mission.routeSegments.length ? "done" : "pending"],
    ["Context Wave", `${mission.payloadNodeIds.length} nodes`, mission.payloadNodeIds.length ? "done" : "pending"],
    ["Documents", mission.summary.documents, mission.documentNodeIds.length ? "done" : "pending"],
    ["First Payload", isLive ? mission.summary.payloadState : "fixture preview", "done"],
    ["Background", isLive ? (mission.live?.finalMaterializationPending ? "pending" : "not reported") : "not executed", isLive && !mission.live?.finalMaterializationPending ? "done" : "pending"],
    ["Final Inspect", isLive ? (mission.live?.terminalForClient ? "terminal" : "not terminal") : "not executed", isLive && mission.live?.terminalForClient ? "done" : "pending"],
  ];
}
