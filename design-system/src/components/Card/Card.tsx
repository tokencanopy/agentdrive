import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cx } from "../../lib/cx";

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Apply the standard internal padding. Maps to `.card-pad`. */
  pad?: boolean;
}

/** Card — `<div class="card">`, the elevated panel used for tiles, panels, and dashboard cards. */
export const Card = forwardRef<HTMLDivElement, CardProps>(function Card({ pad = false, className, ...rest }, ref) {
  return <div ref={ref} className={cx("card", pad && "card-pad", className)} {...rest} />;
});
