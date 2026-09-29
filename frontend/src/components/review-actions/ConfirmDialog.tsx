"use client";

import { useEffect, useId, useRef, type ReactNode } from "react";
import styles from "./ReviewActions.module.css";

interface Props {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  danger?: boolean;
  pending?: boolean;
  confirmDisabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  children?: ReactNode;
}

/**
 * Accessible modal confirmation (M11C task §21). Built on the native
 * `<dialog>` element: `showModal()` makes everything else inert, traps
 * keyboard focus inside, closes on Escape, and the component restores focus
 * to the control that opened it. Focus lands on the *cancel* button so a
 * stray Enter never confirms a terminal action.
 */
export function ConfirmDialog({
  open, title, description, confirmLabel, cancelLabel = "Cancel", danger = false, pending = false,
  confirmDisabled = false, onConfirm, onCancel, children,
}: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const opener = useRef<Element | null>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      opener.current = document.activeElement;
      dialog.showModal();
      cancelRef.current?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
      if (opener.current instanceof HTMLElement) opener.current.focus();
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={styles.dialog}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onCancel={(event) => {
        event.preventDefault();
        if (!pending) onCancel();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        // A native modal dialog makes the page inert but lets Tab leave the
        // document (into browser chrome). Wrap focus explicitly so it cycles.
        const dialog = ref.current;
        if (!dialog) return;
        const focusable = Array.from(
          dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])'),
        );
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (!first || !last) return;
        const active = document.activeElement;
        if (event.shiftKey && (active === first || !dialog.contains(active))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
          event.preventDefault();
          first.focus();
        }
      }}
    >
      <div className={styles.dialogBody}>
        <h2 id={titleId} className={styles.dialogTitle}>{title}</h2>
        <div id={descriptionId} className={styles.note}>{description}</div>
        {children}
        <div className={styles.dialogActions}>
          <button ref={cancelRef} type="button" className={styles.button} onClick={onCancel} disabled={pending}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`${styles.button} ${danger ? styles.danger : styles.primary}`}
            onClick={onConfirm}
            disabled={pending || confirmDisabled}
          >
            {pending ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  );
}
