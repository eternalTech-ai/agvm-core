import type { ReactNode } from "react";

type CockpitShellProps = {
  topbar: ReactNode;
  rail: ReactNode;
  stage: ReactNode;
  timeline: ReactNode;
  modal?: ReactNode;
  visualOption?: string;
  wideStage?: boolean;
};

export function CockpitShell({ topbar, rail, stage, timeline, modal, visualOption = "mint-grid", wideStage = false }: CockpitShellProps) {
  const hasTimeline = Boolean(timeline);
  return (
    <div className={`neural-cockpit shell-v2 shell-option-${visualOption}${wideStage ? " wide-stage" : ""}${hasTimeline ? "" : " timeline-disabled"}`}>
      {topbar}
      <main className="et-workspace shell-v2-workspace">
        {rail}
        {stage}
      </main>
      {hasTimeline ? timeline : null}
      {modal}
    </div>
  );
}
