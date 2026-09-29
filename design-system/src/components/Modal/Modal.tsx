import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cx } from "../../lib/cx";

export type ModalSize = "sm" | "wide";

export interface ModalProps extends HTMLAttributes<HTMLDivElement> {
  /** Width variant. Maps to `.modal-{size}`. Omit for the default width. */
  size?: ModalSize;
  /** Called when the backdrop (scrim) is clicked. The modal box itself stops propagation. */
  onClose?: () => void;
}

/**
 * Modal — `<div class="scrim"><div class="modal …" role="dialog">`. Compose the
 * interior with `Modal.Head` / `Modal.Body` / `Modal.Foot` (or the named
 * `ModalHead`/`ModalBody`/`ModalFoot` exports).
 */
const ModalRoot = forwardRef<HTMLDivElement, ModalProps>(function Modal(
  { size, onClose, className, children, onClick, ...rest },
  ref,
) {
  return (
    <div className="scrim" onClick={onClose ? () => onClose() : undefined}>
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        className={cx("modal", size && `modal-${size}`, className)}
        onClick={(e) => {
          e.stopPropagation();
          onClick?.(e);
        }}
        {...rest}
      >
        {children}
      </div>
    </div>
  );
});

/** Modal header row — `<div class="modal-head">` (kind chip + title + close). */
export const ModalHead = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(function ModalHead(
  { className, ...rest },
  ref,
) {
  return <div ref={ref} className={cx("modal-head", className)} {...rest} />;
});

/** Modal scrolling body — `<div class="modal-body">`. */
export const ModalBody = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(function ModalBody(
  { className, ...rest },
  ref,
) {
  return <div ref={ref} className={cx("modal-body", className)} {...rest} />;
});

/** Modal action footer — `<div class="modal-foot">`. */
export const ModalFoot = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(function ModalFoot(
  { className, ...rest },
  ref,
) {
  return <div ref={ref} className={cx("modal-foot", className)} {...rest} />;
});

export const Modal = Object.assign(ModalRoot, {
  Head: ModalHead,
  Body: ModalBody,
  Foot: ModalFoot,
});
