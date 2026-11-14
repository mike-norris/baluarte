import {
  IconDots,
  IconMenu2,
  IconMoon,
  IconShieldLock,
  IconSun,
  IconX,
} from "@tabler/icons-react";
import { useId, useState } from "react";
import { NavLink, Outlet } from "react-router";

import { AsOfBanner } from "../asof/AsOfBanner";
import { AsOfPicker } from "../asof/AsOfPicker";
import { AsOfProvider } from "../asof/AsOfProvider";
import { useAuth } from "../auth/context";
import { ComplianceAlertBanner } from "../compliance/ComplianceAlertBanner";
import { CollectionAlertBanner } from "../freshness/CollectionAlertBanner";
import { FreshnessIndicator } from "../freshness/FreshnessIndicator";
import { FreshnessProvider } from "../freshness/FreshnessProvider";
import { navGroups, visibleNavItems, type NavItem } from "../navigation";
import { useDeviceClass } from "../ui/device";
import { useTheme } from "../ui/theme";
import { ThemeProvider } from "../ui/ThemeProvider";
import { AccountMenu } from "./AccountMenu";
import { DevelopmentModeBanner } from "./DevelopmentModeBanner";

function NavItems({ items, onNavigate }: { items: readonly NavItem[]; onNavigate?: () => void }) {
  return navGroups.map((group) => {
    const inGroup = items.filter((item) => item.group === group);
    if (inGroup.length === 0) return null;
    return [
      <li key={group} className="nav-section-title" aria-hidden="true">
        {group}
      </li>,
      ...inGroup.map((item) => {
        const Icon = item.icon;
        return (
          <li key={item.path} className="nav-item">
            <NavLink
              to={item.path}
              end={item.path === "/"}
              className="nav-link"
              onClick={onNavigate}
            >
              <span className="nav-link-icon d-inline-block" aria-hidden="true">
                <Icon size={20} className="icon" />
              </span>
              <span className="nav-link-title">{item.label}</span>
            </NavLink>
          </li>
        );
      }),
    ];
  });
}

/** The vertical sidebar: always open from lg up; a menu button opens it on tablets. */
function Sidebar({ items }: { items: readonly NavItem[] }) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  return (
    <aside
      className="navbar navbar-vertical navbar-expand-lg d-none d-md-flex"
      data-bs-theme="dark"
    >
      <div className="container-fluid">
        <button
          type="button"
          className="navbar-toggler"
          aria-controls={menuId}
          aria-expanded={open}
          aria-label={open ? "Close menu" : "Open menu"}
          onClick={() => {
            setOpen((value) => !value);
          }}
        >
          {open ? <IconX size={20} aria-hidden="true" /> : <IconMenu2 size={20} aria-hidden="true" />}
        </button>
        <span className="navbar-brand navbar-brand-autodark">
          <IconShieldLock size={24} className="me-2" aria-hidden="true" />
          Baluarte
        </span>
        <div id={menuId} className={`collapse navbar-collapse ${open ? "show" : ""}`}>
          <nav aria-label="Primary" className="w-100">
            <ul className="navbar-nav pt-lg-3">
              <NavItems
                items={items}
                onNavigate={() => {
                  setOpen(false);
                }}
              />
            </ul>
          </nav>
        </div>
      </div>
    </aside>
  );
}

/** Phones: four sections in a bottom bar and the rest under More. */
function BottomNav({ items }: { items: readonly NavItem[] }) {
  const [moreOpen, setMoreOpen] = useState(false);
  const moreId = useId();
  const titleId = useId();
  const close = () => {
    setMoreOpen(false);
  };
  const bar = items.filter((item) => item.phoneBar);
  const fallback = items.find((item) => item.path === "/inventory");
  const shown = bar.length < 3 && fallback ? [...bar, fallback] : bar;
  return (
    <>
      <nav
        aria-label="Primary"
        className="app-bottom-nav navbar fixed-bottom border-top bg-body p-0"
      >
        <ul className="nav nav-fill w-100">
          {shown.map((item) => {
            const Icon = item.icon;
            return (
              <li key={item.path} className="nav-item">
                <NavLink to={item.path} end={item.path === "/"} className="nav-link">
                  <Icon size={22} aria-hidden="true" />
                  {item.label}
                </NavLink>
              </li>
            );
          })}
          <li className="nav-item">
            <button
              type="button"
              className="nav-link w-100 border-0 bg-transparent"
              aria-controls={moreId}
              aria-expanded={moreOpen}
              onClick={() => {
                setMoreOpen(true);
              }}
            >
              <IconDots size={22} aria-hidden="true" />
              More
            </button>
          </li>
        </ul>
      </nav>
      {moreOpen && (
        <>
          <div
            id={moreId}
            className="offcanvas offcanvas-bottom show h-auto"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            style={{ maxHeight: "90vh", visibility: "visible" }}
          >
            <div className="offcanvas-header">
              <h2 id={titleId} className="offcanvas-title">
                More
              </h2>
              <button
                type="button"
                className="btn-close"
                aria-label="Close"
                onClick={() => {
                  setMoreOpen(false);
                }}
              />
            </div>
            <div className="offcanvas-body pt-0">
              <ul className="nav flex-column">
                {items
                  .filter((item) => !shown.includes(item))
                  .map((item) => {
                    const Icon = item.icon;
                    return (
                      <li key={item.path} className="nav-item">
                        <NavLink to={item.path} className="nav-link gap-2" onClick={close}>
                          <Icon size={20} aria-hidden="true" />
                          {item.label}
                        </NavLink>
                      </li>
                    );
                  })}
              </ul>
            </div>
          </div>
          <div
            className="offcanvas-backdrop fade show"
            aria-hidden="true"
            onClick={() => {
              setMoreOpen(false);
            }}
          />
        </>
      )}
    </>
  );
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="btn btn-ghost-secondary btn-icon d-none d-md-inline-flex"
      aria-label={`Use ${next} theme`}
      onClick={() => {
        setTheme(next);
      }}
    >
      {theme === "dark" ? <IconSun size={18} aria-hidden="true" /> : <IconMoon size={18} aria-hidden="true" />}
    </button>
  );
}

export function Layout() {
  return (
    <ThemeProvider>
      <Shell />
    </ThemeProvider>
  );
}

function Shell() {
  const { session } = useAuth();
  const device = useDeviceClass();
  const user = session.status === "signed_in" ? session.user : null;
  const items = user ? visibleNavItems(user.permissions) : [];
  const phone = device === "phone";

  return (
    <FreshnessProvider enabled={user !== null}>
      <AsOfProvider>
        <a href="#main" className="visually-hidden-focusable btn btn-primary position-absolute m-2">
          Skip to main content
        </a>
        {user?.development_mode === true && <DevelopmentModeBanner />}
        <div className={`page ${phone && user ? "page-has-bottom-nav" : ""}`}>
          {user && !phone && <Sidebar items={items} />}
          <div className="page-wrapper">
            <header className="navbar navbar-expand d-print-none border-bottom">
              <div className="container-xl">
                {(!user || phone) && (
                  <span className="navbar-brand">
                    <IconShieldLock size={24} className="me-2" aria-hidden="true" />
                    Baluarte
                  </span>
                )}
                <div className="navbar-nav flex-row align-items-center gap-2 ms-auto">
                  {user && <AsOfPicker />}
                  <FreshnessIndicator />
                  <ThemeToggle />
                  <AccountMenu />
                </div>
              </div>
            </header>
            {user && <AsOfBanner />}
            {user && <CollectionAlertBanner />}
            {user?.permissions.includes("compliance:read") === true && <ComplianceAlertBanner />}
            <main id="main" tabIndex={-1} className="page-body">
              <div className="container-xl">
                {/* Pages not yet moved to Tabler keep their light look. Removed with Tailwind. */}
                <div data-bs-theme="light" className="legacy-page">
                  <Outlet />
                </div>
              </div>
            </main>
          </div>
        </div>
        {user && phone && <BottomNav items={items} />}
      </AsOfProvider>
    </FreshnessProvider>
  );
}
