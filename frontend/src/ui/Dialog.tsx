import { useId, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useModal } from "./useModal";

interface DialogProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** The buttons; omitted when the body is a form with its own. */
  footer?: ReactNode;
}

/**
 * A Tabler modal driven by React. On phones it fills the screen, so a confirmation has room for
 * the facts it restates.
 */
export function Dialog({ open, title, onClose, children, footer }: DialogProps) {
  const titleId = useId();
  const heading = useModal<HTMLHeadingElement>(open, onClose);
  if (!open) return null;
  return createPortal(
    <>
      <div
        className="modal d-block"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="modal-dialog modal-dialog-centered modal-fullscreen-sm-down">
          <div className="modal-content">
            <div className="modal-header">
              <h2 id={titleId} ref={heading} tabIndex={-1} className="modal-title">
                {title}
              </h2>
              <button type="button" className="btn-close" aria-label="Close" onClick={onClose} />
            </div>
            <div className="modal-body">{children}</div>
            {footer && <div className="modal-footer">{footer}</div>}
          </div>
        </div>
      </div>
      <div className="modal-backdrop show" aria-hidden="true" />
    </>,
    document.body,
  );
}
