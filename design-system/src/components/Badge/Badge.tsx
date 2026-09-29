import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cx } from "../../lib/cx";

export type BadgeTone = "accent" | "info" | "warn" | "danger" | "success";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  /** Solid tone fill for status badges. Maps to `.solid-{tone}`. Omit for the default outline badge. */
  tone?: BadgeTone;
  /** Render a leading live-indicator dot. Maps to `.dot` (`.badge.dot::before`). */
  dot?: boolean;
}

/** Badge — `<span class="badge">`. Small status/metadata pill (versions, sizes, visibility). */
export const Badge = forwardRef<HTMLSpanElement, BadgeProps>(function Badge(
  { tone, dot = false, className, ...rest },
  ref,
) {
  return <span ref={ref} className={cx("badge", tone && `solid-${tone}`, dot && "dot", className)} {...rest} />;
});
