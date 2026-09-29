import { forwardRef } from "react";
import type { InputHTMLAttributes } from "react";
import { cx } from "../../lib/cx";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Monospace variant for paths / IDs / other identifiers. Maps to `.mono`. */
  mono?: boolean;
}

/** Input — `<input class="input">`. Use the `mono` variant for machine-meaningful strings. */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input({ mono = false, className, ...rest }, ref) {
  return <input ref={ref} className={cx("input", mono && "mono", className)} {...rest} />;
});
