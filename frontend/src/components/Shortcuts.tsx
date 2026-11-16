import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { Dialog } from "../ui/Dialog";
import { JUMP_SEARCH_ID } from "./JumpSearch";

/** `g` followed by one of these keys opens the page. */
export const goTo: Record<string, { path: string; label: string }> = {
  d: { path: "/", label: "Dashboard" },
  r: { path: "/reviews", label: "Review queue" },
  f: { path: "/findings", label: "Findings" },
};

const SEQUENCE_MS = 1500;

/** Typing in a field, or a key with a modifier, is never a shortcut. */
function ignored(event: KeyboardEvent): boolean {
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return true;
  const target = event.target as HTMLElement | null;
  if (!target) return false;
  return (
    target.isContentEditable ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) ||
    target.closest('[role="dialog"]') !== null
  );
}

/**
 * Keyboard shortcuts for keyboard users on larger screens: `/` focuses the jump search,
 * `g d`, `g r` and `g f` open pages, and `?` lists them. Off while focus is in a form field.
 */
export function Shortcuts({ paths }: { paths: ReadonlySet<string> }) {
  const navigate = useNavigate();
  const [help, setHelp] = useState(false);
  const pendingG = useRef<number | null>(null);

  useEffect(() => {
    const clear = () => {
      if (pendingG.current !== null) window.clearTimeout(pendingG.current);
      pendingG.current = null;
    };
    const onKey = (event: KeyboardEvent) => {
      if (ignored(event)) return;
      if (pendingG.current !== null) {
        const target = goTo[event.key];
        clear();
        if (target && paths.has(target.path)) {
          event.preventDefault();
          void navigate(target.path);
        }
        return;
      }
      if (event.key === "g") {
        pendingG.current = window.setTimeout(clear, SEQUENCE_MS);
      } else if (event.key === "/") {
        const input = document.getElementById(JUMP_SEARCH_ID);
        if (input) {
          event.preventDefault();
          input.focus();
        }
      } else if (event.key === "?") {
        event.preventDefault();
        setHelp(true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      clear();
    };
  }, [navigate, paths]);

  return (
    <Dialog
      open={help}
      title="Keyboard shortcuts"
      onClose={() => {
        setHelp(false);
      }}
    >
      <p className="text-secondary">Shortcuts are off while you type in a field.</p>
      <dl className="d-grid cols-dl row-gap-2 mb-0">
        <dt>
          <kbd>/</kbd>
        </dt>
        <dd className="mb-0">Jump to a project, record or page</dd>
        {Object.entries(goTo)
          .filter(([, target]) => paths.has(target.path))
          .map(([key, target]) => (
            <div key={key} className="d-contents">
              <dt>
                <kbd>g</kbd> then <kbd>{key}</kbd>
              </dt>
              <dd className="mb-0">{`Open ${target.label.toLowerCase()}`}</dd>
            </div>
          ))}
        <dt>
          <kbd>?</kbd>
        </dt>
        <dd className="mb-0">Show these shortcuts</dd>
      </dl>
    </Dialog>
  );
}
