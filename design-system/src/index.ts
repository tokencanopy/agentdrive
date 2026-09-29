// AgentDrive design system — public entry.
//
// Thin React wrappers over the canonical agentdrive.css. Each component emits the
// real CSS class names, so anything composed from these maps 1:1 back to a Jinja
// template. Slice 1 ships the cx helper + the 7 core components; the full catalog
// follows in Slice 4+.
export const DESIGN_SYSTEM_VERSION = "0.0.0";

export { cx } from "./lib/cx";
export type { ClassValue } from "./lib/cx";

export * from "./components";
