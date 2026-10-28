import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth/context";
import { navItems } from "../navigation";
import { AccountMenu } from "./AccountMenu";

const linkBase =
  "block rounded px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600";

export function Layout() {
  const { session } = useAuth();

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <a
        href="#main"
        className="sr-only rounded bg-white px-3 py-2 focus:not-sr-only focus:absolute focus:top-2 focus:left-2"
      >
        Skip to main content
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-col gap-3 px-4 py-3 md:flex-row md:items-center">
          <p className="text-lg font-semibold">Baluarte</p>
          {session.status === "signed_in" && (
            <nav aria-label="Primary">
              <ul className="flex flex-wrap gap-1">
                {navItems.map((item) => (
                  <li key={item.path}>
                    <NavLink
                      to={item.path}
                      end={item.path === "/"}
                      className={({ isActive }) =>
                        `${linkBase} ${isActive ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100"}`
                      }
                    >
                      {item.label}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </nav>
          )}
          <AccountMenu />
        </div>
      </header>
      <main id="main" tabIndex={-1} className="mx-auto max-w-6xl px-4 py-8 focus:outline-none">
        <Outlet />
      </main>
    </div>
  );
}
