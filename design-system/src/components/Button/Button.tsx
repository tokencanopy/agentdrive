import { forwardRef } from "react";
import type { ButtonHTMLAttributes } from "react";
import { cx } from "../../lib/cx";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "ink";
export type ButtonSize = "sm" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual role. Only one `primary` per visible region. Maps to `.btn-{variant}`. */
  variant?: ButtonVariant;
  /** Optional size modifier. Maps to `.btn-{size}`. */
  size?: ButtonSize;
  /** Monospace label, for buttons whose text is an identifier. Maps to `.btn-mono`. */
  mono?: boolean;
}

/**
 * Button — `<button class="btn btn-{variant} …">`. Always a real `<button>`
 * (a link-styled action is still a button, per the design rules). `type`
 * defaults to `"button"` so it never accidentally submits a form.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size, mono = false, type = "button", className, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx("btn", `btn-${variant}`, size && `btn-${size}`, mono && "btn-mono", className)}
      {...rest}
    />
  );
});
