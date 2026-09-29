import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cx } from "../../lib/cx";

// Feedback / status primitives over the existing agentdrive.css classes.

export type AlertTone = "info" | "warn" | "danger" | "success";
export interface AlertProps extends HTMLAttributes<HTMLDivElement> {
  /** Inline banner tone. Maps to `.alert-{tone}`. */
  tone?: AlertTone;
}
/** Alert — `<div class="alert alert-{tone}" role="alert">`. Inline banner at the top of a form/section. */
export const Alert = forwardRef<HTMLDivElement, AlertProps>(function Alert({ tone = "info", className, ...rest }, ref) {
  return <div ref={ref} role="alert" className={cx("alert", `alert-${tone}`, className)} {...rest} />;
});

export type ToastTone = "success" | "danger";
export interface ToastProps extends HTMLAttributes<HTMLDivElement> {
  /** Maps to `.toast.{tone}`. Omit for the neutral toast. */
  tone?: ToastTone;
}
/** Toast — `<div class="toast …">`. Compose with `Toast.Icon` / `Toast.Title` / `Toast.Sub`. */
const ToastRoot = forwardRef<HTMLDivElement, ToastProps>(function Toast({ tone, className, ...rest }, ref) {
  return <div ref={ref} className={cx("toast", tone, className)} {...rest} />;
});
export const ToastIcon = forwardRef<HTMLSpanElement, HTMLAttributes<HTMLSpanElement>>(function ToastIcon(
  { className, ...rest },
  ref,
) {
  return <span ref={ref} className={cx("toast-ico", className)} {...rest} />;
});
export const ToastTitle = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(function ToastTitle(
  { className, ...rest },
  ref,
) {
  return <div ref={ref} className={cx("toast-ttl", className)} {...rest} />;
});
export const ToastSub = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(function ToastSub(
  { className, ...rest },
  ref,
) {
  return <div ref={ref} className={cx("toast-sub", className)} {...rest} />;
});
export const Toast = Object.assign(ToastRoot, { Icon: ToastIcon, Title: ToastTitle, Sub: ToastSub });

export interface ProgressProps extends Omit<HTMLAttributes<HTMLDivElement>, "role"> {
  /** Fill percent 0–100. Ignored when `indeterminate`. */
  value?: number;
  /** Ambient/unknown progress. Maps to `.progress.indeterminate`. */
  indeterminate?: boolean;
}
/** Progress — `<div class="progress"><div class="bar"/></div>`. */
export const Progress = forwardRef<HTMLDivElement, ProgressProps>(function Progress(
  { value = 0, indeterminate = false, className, ...rest },
  ref,
) {
  return (
    <div
      ref={ref}
      role="progressbar"
      aria-valuenow={indeterminate ? undefined : value}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cx("progress", indeterminate && "indeterminate", className)}
      {...rest}
    >
      <div className="bar" style={indeterminate ? undefined : { width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
});

/** Spinner — `<span class="spinner">`. Inline loading inside a button or sentence. */
export const Spinner = forwardRef<HTMLSpanElement, HTMLAttributes<HTMLSpanElement>>(function Spinner(
  { className, ...rest },
  ref,
) {
  return <span ref={ref} className={cx("spinner", className)} {...rest} />;
});

/** Pulse — `<span class="pulse">`. Inline "live now" dot. */
export const Pulse = forwardRef<HTMLSpanElement, HTMLAttributes<HTMLSpanElement>>(function Pulse(
  { className, ...rest },
  ref,
) {
  return <span ref={ref} className={cx("pulse", className)} {...rest} />;
});

export type SkeletonVariant = "line" | "title" | "thumb" | "btn" | "avatar";
export interface SkeletonProps extends HTMLAttributes<HTMLDivElement> {
  /** Shape. Maps to `.skeleton.{variant}`. */
  variant?: SkeletonVariant;
  /** Line length (only meaningful for `variant="line"`). Maps to `.skeleton.line.{width}`. */
  width?: "short" | "med" | "long";
}
/** Skeleton — `<div class="skeleton …">`. Placeholder while content is in flight. */
export const Skeleton = forwardRef<HTMLDivElement, SkeletonProps>(function Skeleton(
  { variant = "line", width, className, ...rest },
  ref,
) {
  return <div ref={ref} className={cx("skeleton", variant, variant === "line" && width, className)} {...rest} />;
});

/** Empty — `<div class="empty">`. Zero-state inside a table, card, or modal. */
export const Empty = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(function Empty(
  { className, ...rest },
  ref,
) {
  return <div ref={ref} className={cx("empty", className)} {...rest} />;
});
