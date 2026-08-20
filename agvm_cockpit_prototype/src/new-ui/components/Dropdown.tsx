import { ChevronDown } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export type DropdownOption<TValue extends string = string> = {
  value: TValue;
  label: string;
  meta?: string;
  disabled?: boolean;
};

type DropdownProps<TValue extends string = string> = {
  ariaLabel?: string;
  className?: string;
  disabled?: boolean;
  icon?: ReactNode;
  label: string;
  menuWidth?: "trigger" | "wide";
  onChange: (value: TValue) => void;
  options: DropdownOption<TValue>[];
  value: TValue;
};

export function Dropdown<TValue extends string = string>({
  ariaLabel,
  className,
  disabled,
  icon,
  label,
  menuWidth = "trigger",
  onChange,
  options,
  value,
}: DropdownProps<TValue>) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [menuRect, setMenuRect] = useState<{ left: number; maxHeight: number; top: number; width: number } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const selected = useMemo(() => options.find((option) => option.value === value) || options[0], [options, value]);
  const isDisabled = Boolean(disabled || !options.length);
  const selectedLabel = selected?.label || "No options";
  const selectedMeta = selected?.meta || "";

  useEffect(() => {
    if (!open) return undefined;
    function updateMenuRect() {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = menuWidth === "wide" ? Math.min(Math.max(rect.width, 420), window.innerWidth - 24) : rect.width;
      const top = Math.min(rect.bottom + 8, window.innerHeight - 80);
      setMenuRect({
        left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
        maxHeight: Math.max(160, window.innerHeight - top - 14),
        top,
        width,
      });
    }
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node | null;
      if (target && (rootRef.current?.contains(target) || menuRef.current?.contains(target))) return;
      setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    updateMenuRect();
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", updateMenuRect);
    window.addEventListener("scroll", updateMenuRect, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", updateMenuRect);
      window.removeEventListener("scroll", updateMenuRect, true);
    };
  }, [menuWidth, open]);

  return (
    <div className={`et-dropdown ${open ? "open" : ""} ${className || ""}`} ref={rootRef}>
      <button
        aria-controls={`${id}-menu`}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel || label}
        className="et-dropdown-trigger"
        disabled={isDisabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            if (!isDisabled) setOpen(true);
          }
        }}
        ref={triggerRef}
        title={selectedMeta ? `${selectedLabel} - ${selectedMeta}` : selectedLabel}
        type="button"
      >
        <span className="et-dropdown-content">
          <span className="et-dropdown-label">
            {icon ? <span className="et-dropdown-icon">{icon}</span> : null}
            {label}
          </span>
          <strong>{selectedLabel}</strong>
          {selectedMeta ? <em>{selectedMeta}</em> : null}
        </span>
        <ChevronDown className="et-dropdown-chevron" size={15} />
      </button>
      {open && menuRect && typeof document !== "undefined"
        ? createPortal(
            <div
              className="et-dropdown-menu"
              id={`${id}-menu`}
              ref={menuRef}
              role="listbox"
              style={{ left: menuRect.left, maxHeight: menuRect.maxHeight, top: menuRect.top, width: menuRect.width }}
              tabIndex={-1}
            >
              {options.map((option) => (
                <button
                  aria-selected={option.value === value}
                  className={option.value === value ? "selected" : ""}
                  disabled={option.disabled}
                  key={option.value}
                  onClick={() => {
                    setOpen(false);
                    if (option.value !== value) onChange(option.value);
                  }}
                  role="option"
                  title={option.meta ? `${option.label} - ${option.meta}` : option.label}
                  type="button"
                >
                  <strong>{option.label}</strong>
                  {option.meta ? <span>{option.meta}</span> : null}
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
