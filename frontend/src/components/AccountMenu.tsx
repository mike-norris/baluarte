import { IconLogout, IconMoon, IconSun } from "@tabler/icons-react";
import { useId, useState } from "react";

import { useAuth } from "../auth/context";
import { displayName } from "../auth/session";
import { useTheme } from "../ui/theme";
import { useDropdown } from "../ui/useDropdown";

function initials(name: string): string {
  const parts = name.split(/[\s@._-]+/).filter(Boolean);
  const [first = "", second = ""] = parts;
  return (second ? first.charAt(0) + second.charAt(0) : name.slice(0, 2)).toUpperCase();
}

/** The account dropdown: who is signed in, their roles, the theme and signing out. */
export function AccountMenu() {
  const { session, signOut } = useAuth();
  const { theme, setTheme } = useTheme();
  const { open, setOpen, root, trigger } = useDropdown<HTMLDivElement, HTMLButtonElement>();
  const [failed, setFailed] = useState(false);
  const menuId = useId();

  if (session.status !== "signed_in") return null;
  const { user } = session;
  const name = displayName(user);

  const handleSignOut = () => {
    setFailed(false);
    signOut().catch(() => {
      setFailed(true);
    });
  };

  return (
    <div ref={root} className="nav-item dropdown">
      <button
        ref={trigger}
        type="button"
        aria-label={`Account: ${name}`}
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => {
          setOpen((value) => !value);
        }}
        className="btn btn-ghost-secondary d-flex align-items-center gap-2 px-1"
      >
        <span className="avatar avatar-sm" aria-hidden="true">
          {initials(name)}
        </span>
        <span className="d-none d-xl-block text-start lh-sm">
          <span className="d-block">{name}</span>
          <span className="d-block small text-secondary">{user.roles.join(", ")}</span>
        </span>
      </button>
      {open && (
        <div
          id={menuId}
          className="dropdown-menu dropdown-menu-end show"
          style={{ right: 0, left: "auto", minWidth: "15rem" }}
        >
          <div className="dropdown-header">
            <div className="fw-medium text-body">{name}</div>
            {user.email && <div className="small text-secondary">{user.email}</div>}
            <div className="mt-1 d-flex flex-wrap gap-1">
              {user.roles.map((role) => (
                <span
                  key={role}
                  className="badge bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle"
                >
                  {role}
                </span>
              ))}
            </div>
          </div>
          <div className="dropdown-divider" />
          <button
            type="button"
            className="dropdown-item"
            onClick={() => {
              setTheme(theme === "dark" ? "light" : "dark");
            }}
          >
            {theme === "dark" ? (
              <IconSun size={18} className="icon dropdown-item-icon" aria-hidden="true" />
            ) : (
              <IconMoon size={18} className="icon dropdown-item-icon" aria-hidden="true" />
            )}
            {theme === "dark" ? "Use light theme" : "Use dark theme"}
          </button>
          <button type="button" className="dropdown-item" onClick={handleSignOut}>
            <IconLogout size={18} className="icon dropdown-item-icon" aria-hidden="true" />
            Sign out
          </button>
          {failed && (
            <p role="alert" className="text-danger-emphasis small px-3 mb-1">
              Sign-out failed. Try again.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
