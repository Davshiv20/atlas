import type { ButtonHTMLAttributes, ReactNode } from "react";

/**
 * The one button.
 *
 * Before this, 38 raw `<button>` elements each wrote their own class string,
 * producing 17 distinct padding combinations across the console — which is why
 * two buttons sitting on the same row rarely shared a height or a shape. Height
 * here is fixed per size rather than derived from padding and font, so a row of
 * buttons aligns regardless of how long the labels are.
 *
 * Keyboard hints deliberately live *outside* the button. A bordered chip inside
 * a filled control is what made the previous set read as lumpy; the hint is a
 * sibling now, rendered by whatever owns the shortcut.
 */

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

const BASE =
  "inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-[--radius-control] " +
  "border font-medium leading-none transition-[background-color,transform] duration-100 " +
  // A 3% inset on press. Nothing lifts, glows, or ripples: depth in this
  // interface is carried by one hairline, so a button that bloomed on click
  // would be the only object on screen behaving like a different system. It is
  // also the only feedback a pointer gets before the row it acts on changes.
  "active:scale-[0.97] " +
  "disabled:cursor-not-allowed disabled:opacity-45";

const VARIANTS: Record<ButtonVariant, string> = {
  // The primary action carries the one accent hue. Nothing that reports a
  // status is allowed to use it.
  primary: "border-transparent bg-cta text-cta-ink hover:bg-cta-hover",
  secondary: "border-line-strong bg-canvas text-ink hover:bg-surface",
  ghost: "border-transparent bg-transparent text-ink-2 hover:bg-raised hover:text-ink",
  // Destructive reads as quiet until hovered: rejecting is common here and a
  // permanently red button trains the eye to ignore it.
  danger: "border-transparent bg-transparent text-red hover:bg-red-soft",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-meta",
  md: "h-[34px] px-3.5 text-body",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children?: ReactNode;
}

export function Button({
  variant = "secondary",
  size = "md",
  className,
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={[BASE, VARIANTS[variant], SIZES[size], className].filter(Boolean).join(" ")}
      {...props}
    />
  );
}

/**
 * A keyboard hint. Sits beside the control it triggers, never inside it.
 */
export function Key({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-[17px] min-w-[17px] items-center justify-center rounded-[3px] border border-line-strong bg-canvas px-1 font-mono text-badge text-ink-3">
      {children}
    </kbd>
  );
}
