import { forwardRef } from "react";
import type { ButtonHTMLAttributes, HTMLAttributes } from "react";
import { cx } from "../../lib/cx";

export interface PillProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Selected filter/sort state. Maps to `.pill.active`. */
  active?: boolean;
}
/** Pill — `<button class="pill …">`. Kind/category filter or sort toggle. */
export const Pill = forwardRef<HTMLButtonElement, PillProps>(function Pill(
  { active = false, type = "button", className, ...rest },
  ref,
) {
  return <button ref={ref} type={type} className={cx("pill", active && "active", className)} {...rest} />;
});

/** Tabs — `<div class="tabs">` of `Tabs.Tab` buttons (the active one carries `.active`). */
const TabsRoot = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(function Tabs({ className, ...rest }, ref) {
  return <div ref={ref} role="tablist" className={cx("tabs", className)} {...rest} />;
});
export interface TabProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** The selected tab. Maps to `.active`. */
  active?: boolean;
}
export const Tab = forwardRef<HTMLButtonElement, TabProps>(function Tab(
  { active = false, type = "button", className, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      role="tab"
      aria-selected={active}
      className={cx(active && "active", className)}
      {...rest}
    />
  );
});
export const Tabs = Object.assign(TabsRoot, { Tab });

export type AvatarSize = "sm" | "lg" | "xl";
export interface AvatarProps extends Omit<HTMLAttributes<HTMLSpanElement>, "color"> {
  /** Deterministic palette slot 0–7 (hash the publisher id). Maps to `data-h`. */
  hash: number;
  /** Square instead of round. Maps to `.av.sq`. */
  square?: boolean;
  /** Size modifier. Maps to `.av.{size}`. */
  size?: AvatarSize;
}
/** Avatar — `<span class="av" data-h="0–7">`. Deterministic publisher color from a hash. */
export const Avatar = forwardRef<HTMLSpanElement, AvatarProps>(function Avatar(
  { hash, square = false, size, className, ...rest },
  ref,
) {
  return (
    <span ref={ref} className={cx("av", square && "sq", size, className)} {...rest} data-h={((hash % 8) + 8) % 8} />
  );
});
