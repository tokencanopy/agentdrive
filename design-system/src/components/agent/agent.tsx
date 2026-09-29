import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cx } from "../../lib/cx";

// Agent-native primitives. AgentTag/Visibility carry a colored dot (via CSS
// ::before); Console is an INK surface for code/MCP snippets (two-surface rule).

export type AgentSource = "user" | "gpt" | "claude";
export interface AgentTagProps extends HTMLAttributes<HTMLSpanElement> {
  /** Who wrote it — sets the dot color. Maps to `.agent-tag.{source}`. Default gold (an unspecified agent). */
  source?: AgentSource;
}
/** AgentTag — `<span class="agent-tag …">`. Provenance for agent-written artifacts. */
export const AgentTag = forwardRef<HTMLSpanElement, AgentTagProps>(function AgentTag(
  { source, className, ...rest },
  ref,
) {
  return <span ref={ref} className={cx("agent-tag", source, className)} {...rest} />;
});

export type Visibility = "public" | "private" | "shared";
export interface VisibilityPillProps extends HTMLAttributes<HTMLSpanElement> {
  /** Visibility state — also drives the leading colored dot. Maps to `.vis.{state}`. */
  state: Visibility;
}
/** VisibilityPill — `<span class="vis {state}">`. Renders its colored dot automatically. */
export const VisibilityPill = forwardRef<HTMLSpanElement, VisibilityPillProps>(function VisibilityPill(
  { state, className, children, ...rest },
  ref,
) {
  return (
    <span ref={ref} className={cx("vis", state, className)} {...rest}>
      {children ?? state}
    </span>
  );
});

/**
 * Console — `<pre class="console">`, the INK surface for code / MCP / API
 * snippets. Color the parts with `Console.Prompt` / `Console.Comment` /
 * `Console.String` / `Console.Key`.
 */
const ConsoleRoot = forwardRef<HTMLPreElement, HTMLAttributes<HTMLPreElement>>(function Console(
  { className, ...rest },
  ref,
) {
  return <pre ref={ref} className={cx("console", className)} {...rest} />;
});
const cspan = (cls: string) =>
  forwardRef<HTMLSpanElement, HTMLAttributes<HTMLSpanElement>>(function ConsoleSpan({ className, ...rest }, ref) {
    return <span ref={ref} className={cx(cls, className)} {...rest} />;
  });
export const ConsolePrompt = cspan("c-prompt");
export const ConsoleComment = cspan("c-comment");
export const ConsoleString = cspan("c-string");
export const ConsoleKey = cspan("c-key");
export const Console = Object.assign(ConsoleRoot, {
  Prompt: ConsolePrompt,
  Comment: ConsoleComment,
  String: ConsoleString,
  Key: ConsoleKey,
});
