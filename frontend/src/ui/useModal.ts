import { useEffect, useEffectEvent, useRef } from "react";

/**
 * Behaviour shared by dialogs and offcanvas panels while open: the app behind becomes inert,
 * focus moves to the panel's heading, Escape closes, and focus returns to what opened it.
 */
export function useModal<Heading extends HTMLElement>(open: boolean, onClose: () => void) {
  const heading = useRef<Heading>(null);
  const close = useEffectEvent(() => {
    onClose();
  });

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const app = document.getElementById("root");
    app?.setAttribute("inert", "");
    document.body.classList.add("modal-open");
    heading.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      app?.removeAttribute("inert");
      document.body.classList.remove("modal-open");
      opener?.focus();
    };
  }, [open]);

  return heading;
}
