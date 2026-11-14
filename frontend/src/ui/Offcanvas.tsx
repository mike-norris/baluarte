import { useId, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useModal } from "./useModal";

interface OffcanvasProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  placement?: "end" | "start" | "bottom";
}

/** A side panel for a form or details, keeping the page behind it in view. */
export function Offcanvas({ open, title, onClose, children, placement = "end" }: OffcanvasProps) {
  const titleId = useId();
  const heading = useModal<HTMLHeadingElement>(open, onClose);
  if (!open) return null;
  return createPortal(
    <>
      <div
        className={`offcanvas offcanvas-${placement} show`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        style={{ visibility: "visible" }}
      >
        <div className="offcanvas-header">
          <h2 id={titleId} ref={heading} tabIndex={-1} className="offcanvas-title">
            {title}
          </h2>
          <button type="button" className="btn-close" aria-label="Close" onClick={onClose} />
        </div>
        <div className="offcanvas-body">{children}</div>
      </div>
      <div className="offcanvas-backdrop fade show" aria-hidden="true" onClick={onClose} />
    </>,
    document.body,
  );
}
