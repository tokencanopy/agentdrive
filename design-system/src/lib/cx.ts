// Zero-dependency className joiner. Filters out falsy parts so component
// modifier classes can be expressed as `cond && "btn-sm"`. Intentionally tiny —
// the design system never pulls in clsx for this.
export type ClassValue = string | number | false | null | undefined;

export function cx(...parts: ClassValue[]): string {
  return parts.filter(Boolean).join(" ");
}
