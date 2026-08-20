import { Lock, type LucideIcon } from "lucide-react";
import type { ReactNode, Ref } from "react";

export type ProductMetric = {
  label: string;
  value: string;
  detail?: string;
};

export type ProductAction = {
  disabled?: boolean;
  icon?: LucideIcon;
  label: string;
  onClick?: () => void;
  reason: string;
};

export function ProductPageFrame({
  actions,
  bodyRef,
  children,
  chrome = "full",
  className = "",
  eyebrow,
  icon: Icon,
  intent,
  metrics,
  mode,
  status,
  title,
}: {
  actions: ProductAction[];
  bodyRef?: Ref<HTMLDivElement>;
  children: ReactNode;
  chrome?: "full" | "embedded";
  className?: string;
  eyebrow: string;
  icon: LucideIcon;
  intent: string;
  metrics: ProductMetric[];
  mode: string;
  status: string;
  title: string;
}) {
  return (
    <section className={`et-stage mode-stage mode-stage-${mode} product-page-frame product-page-frame-${chrome} ${className}`.trim()}>
      <div className="mode-stage-ambient" aria-hidden="true" />
      {chrome === "full" ? (
        <header className="mode-stage-header product-page-header">
          <div className="mode-title-block product-page-title">
            <span>{eyebrow}</span>
            <h1>
              <Icon size={24} />
              {title}
            </h1>
            <p>{intent}</p>
          </div>
          {metrics.length ? <ProductMetricStrip metrics={metrics} /> : null}
          <div className="mode-state-card product-state-card">
            <span>Current state</span>
            <strong>{status}</strong>
          </div>
        </header>
      ) : null}

      <div className="mode-stage-body product-page-body" ref={bodyRef}>
        {children}
      </div>
      {chrome === "full" && actions.length ? <ProductActionStrip actions={actions} /> : null}
    </section>
  );
}

export function ProductMetricStrip({ metrics }: { metrics: ProductMetric[] }) {
  return (
    <div className="mode-metrics product-metrics">
      {metrics.map((metric) => (
        <div className="mode-metric-card product-metric-card" key={metric.label}>
          <span>{metric.label}</span>
          <strong>{metric.value}</strong>
          {metric.detail ? <em>{metric.detail}</em> : null}
        </div>
      ))}
    </div>
  );
}

export function ProductActionStrip({ actions }: { actions: ProductAction[] }) {
  return (
    <footer className="mode-actions product-actions">
      {actions.map((action) => (
        <ProductActionButton action={action} key={action.label} />
      ))}
    </footer>
  );
}

function ProductActionButton({ action }: { action: ProductAction }) {
  const Icon = action.icon || Lock;
  const disabled = action.disabled ?? !action.onClick;
  return (
    <button disabled={disabled} onClick={action.onClick} title={action.reason} type="button">
      <Icon size={14} />
      <span>{action.label}</span>
      <small>{action.reason}</small>
    </button>
  );
}
