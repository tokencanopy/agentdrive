import { forwardRef } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";
import { cx } from "../../lib/cx";

// Form controls. In agentdrive.css the `.check` / `.radio` / `.toggle` classes
// live on a <label> WRAPPER and style the <input> inside it (appearance:none +
// the AgentDrive look) — so each component renders that label wrapper, not a
// bare classed input, and forwards the ref to the real input. `className`
// extends the label root.

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  /** Inline label text rendered next to the box. */
  children?: ReactNode;
}
/** Checkbox — `<label class="check"><input type="checkbox"> {children}</label>`. */
export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { className, children, ...rest },
  ref,
) {
  return (
    <label className={cx("check", className)}>
      <input type="checkbox" ref={ref} {...rest} />
      {children != null && <span>{children}</span>}
    </label>
  );
});

export interface RadioProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  /** Inline label text rendered next to the radio. */
  children?: ReactNode;
}
/** Radio — `<label class="radio"><input type="radio"> {children}</label>`. */
export const Radio = forwardRef<HTMLInputElement, RadioProps>(function Radio({ className, children, ...rest }, ref) {
  return (
    <label className={cx("radio", className)}>
      <input type="radio" ref={ref} {...rest} />
      {children != null && <span>{children}</span>}
    </label>
  );
});

export type ToggleProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "children">;
/**
 * Toggle — `<label class="toggle"><input type="checkbox"><span class="slider"/></label>`,
 * the sliding on/off switch. The hidden input carries the state; the `.slider`
 * span is the visual track + thumb. Pair it with adjacent text for a labelled row.
 */
export const Toggle = forwardRef<HTMLInputElement, ToggleProps>(function Toggle({ className, ...rest }, ref) {
  return (
    <label className={cx("toggle", className)}>
      <input type="checkbox" role="switch" ref={ref} {...rest} />
      <span className="slider" />
    </label>
  );
});
