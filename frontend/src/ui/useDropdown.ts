import { useEffect, useRef, useState } from "react";

/**
 * Open state for a React-controlled Tabler dropdown: Escape closes it and returns focus to the
 * trigger, and a click outside closes it. No Bootstrap script is used.
 */
export function useDropdown<Root extends HTMLElement, Trigger extends HTMLElement>() {
  const [open, setOpen] = useState(false);
  const root = useRef<Root>(null);
  const trigger = useRef<Trigger>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus();
    };
    const onPointer = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  return { open, setOpen, root, trigger };
}
