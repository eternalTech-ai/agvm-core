import type { ReactNode } from "react";

export type SegmentedControlOption<TValue extends string = string> = {
  disabled?: boolean;
  icon?: ReactNode;
  label: string;
  meta?: string;
  title?: string;
  value: TValue;
};

type SegmentedControlProps<TValue extends string = string> = {
  ariaLabel?: string;
  className?: string;
  density?: "compact" | "normal";
  label: string;
  onChange: (value: TValue) => void;
  options: SegmentedControlOption<TValue>[];
  value: TValue;
};

export function SegmentedControl<TValue extends string = string>({
  ariaLabel,
  className = "",
  density = "normal",
  label,
  onChange,
  options,
  value,
}: SegmentedControlProps<TValue>) {
  return (
    <fieldset className={`et-control-group et-segmented-field et-segmented-field-${density} ${className}`.trim()}>
      <legend>{label}</legend>
      <div aria-label={ariaLabel || label} className="et-segmented-control" role="group">
        {options.map((option) => {
          const active = option.value === value;
          return (
            <button
              aria-pressed={active}
              className={active ? "active" : ""}
              disabled={option.disabled}
              key={option.value}
              onClick={() => {
                if (!active && !option.disabled) onChange(option.value);
              }}
              title={option.title || option.meta || option.label}
              type="button"
            >
              {option.icon ? <span className="et-segmented-icon">{option.icon}</span> : null}
              <span>{option.label}</span>
              {option.meta ? <em>{option.meta}</em> : null}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}
