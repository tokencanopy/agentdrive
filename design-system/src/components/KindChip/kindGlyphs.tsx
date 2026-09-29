import type { ReactElement } from "react";

// The 8 artifact kinds. `data-k` drives the color binding in agentdrive.css
// (`.kind[data-k="…"]`), and each kind pairs that color with a distinct glyph
// so the signal is robust to color-vision deficiency (ACCESSIBILITY.md: color is
// never the only signal). Glyphs are transcribed verbatim from the design system
// reference (design/12-components.html) — do not restyle.
export type Kind = "md" | "code" | "image" | "video" | "dataset" | "skill" | "bundle" | "folder";

export const KINDS: readonly Kind[] = ["md", "code", "image", "video", "dataset", "skill", "bundle", "folder"];

const svg = (children: ReactElement | ReactElement[], extra?: Record<string, string>): ReactElement => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" {...extra}>
    {children}
  </svg>
);

export const KIND_GLYPHS: Record<Kind, ReactElement> = {
  md: svg(<path d="M5 4h10l4 4v12H5z" />, { strokeLinejoin: "round" }),
  code: svg(<path d="M10 8l-4 4 4 4M14 8l4 4-4 4" />, { strokeLinecap: "round" }),
  image: svg([<rect key="r" x="3" y="5" width="18" height="14" rx="2" />, <circle key="c" cx="9" cy="11" r="1.5" />]),
  video: svg([
    <rect key="r" x="3" y="5" width="18" height="14" rx="2" />,
    <path key="p" d="M10 9l5 3-5 3z" fill="currentColor" />,
  ]),
  dataset: svg([
    <ellipse key="e" cx="12" cy="6" rx="7" ry="2.5" />,
    <path key="p" d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6" />,
  ]),
  skill: svg(<path d="M12 3l8 4-8 4-8-4zM4 11l8 4 8-4" />, { strokeLinejoin: "round" }),
  bundle: svg([<rect key="r" x="4" y="7" width="16" height="13" rx="2" />, <path key="p" d="M4 11h16M9 7v-3h6v3" />], {
    strokeLinejoin: "round",
  }),
  folder: svg(<path d="M4 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" />, {
    strokeLinejoin: "round",
  }),
};
