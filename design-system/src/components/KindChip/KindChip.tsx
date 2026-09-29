import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cx } from "../../lib/cx";
import { KIND_GLYPHS } from "./kindGlyphs";
import type { Kind } from "./kindGlyphs";

export type { Kind } from "./kindGlyphs";
export { KINDS } from "./kindGlyphs";

export interface KindChipProps extends Omit<HTMLAttributes<HTMLSpanElement>, "children"> {
  /** Artifact kind. Drives the `data-k` color binding AND the paired glyph. */
  kind: Kind;
  /** Label text. Defaults to the kind name; override for sub-types (e.g. `jsonl`, `python`). */
  label?: string;
}

/**
 * KindChip — `<span class="kind" data-k="{kind}"><glyph/> {label}</span>`.
 * Color (via `data-k`) is always paired with the kind glyph and a text label so
 * the signal survives color-vision deficiency. `data-k` is component-owned and
 * cannot be overridden by spread props.
 */
export const KindChip = forwardRef<HTMLSpanElement, KindChipProps>(function KindChip(
  { kind, label, className, ...rest },
  ref,
) {
  return (
    <span ref={ref} className={cx("kind", className)} {...rest} data-k={kind}>
      {KIND_GLYPHS[kind]}
      {label ?? kind}
    </span>
  );
});
