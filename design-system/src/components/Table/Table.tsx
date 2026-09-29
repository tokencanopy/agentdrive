import { forwardRef } from "react";
import type { TableHTMLAttributes } from "react";
import { cx } from "../../lib/cx";

export interface TableProps extends TableHTMLAttributes<HTMLTableElement> {
  /** Fixed-layout truncation variant for long-data tables. Maps to `.tbl-trunc`. */
  trunc?: boolean;
}

/**
 * Table — `<table class="tbl">`. Dense by default. Compose with native
 * `<thead>/<tbody>/<tr>/<th>/<td>`; put `className="mono"` on cells holding
 * identifiers (paths, hashes, timestamps).
 */
export const Table = forwardRef<HTMLTableElement, TableProps>(function Table({ trunc = false, className, ...rest }, ref) {
  return <table ref={ref} className={cx("tbl", trunc && "tbl-trunc", className)} {...rest} />;
});
