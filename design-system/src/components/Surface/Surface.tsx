import { forwardRef } from "react";
import type { HTMLAttributes } from "react";

export interface SurfaceProps extends HTMLAttributes<HTMLDivElement> {
  /**
   * Token theme for this subtree. Sets `data-theme`, which re-resolves every
   * AgentDrive token (`--bg`, `--fg`, `--accent`, the kind hues, …) to the cream
   * or dark set for everything inside. Omit to inherit the page theme.
   */
  theme?: "light" | "dark";
}

/**
 * Surface — a themed container. Sets `data-theme` on a div and paints the
 * matching `--bg`/`--fg`, so you can drop a **dark section** into an otherwise
 * light page (or vice-versa) and have every nested component re-theme itself.
 * The app normally sets `data-theme` on `<html>`; Surface scopes it to a region.
 */
export const Surface = forwardRef<HTMLDivElement, SurfaceProps>(function Surface(
  { theme, style, ...rest },
  ref,
) {
  return <div ref={ref} data-theme={theme} style={{ background: "var(--bg)", color: "var(--fg)", ...style }} {...rest} />;
});
