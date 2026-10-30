import type { ReactNode } from "react";
import type { RouteObject } from "react-router";

import { AuthProvider } from "./auth/AuthProvider";
import { RequireSession } from "./auth/RequireSession";
import { Layout } from "./components/Layout";
import { navItems } from "./navigation";
import { DashboardPage } from "./pages/DashboardPage";
import { EvidencePage } from "./pages/EvidencePage";
import { ExceptionDetailPage } from "./pages/ExceptionDetailPage";
import { ExceptionsPage } from "./pages/ExceptionsPage";
import { FindingDetailPage } from "./pages/FindingDetailPage";
import { FindingsPage } from "./pages/FindingsPage";
import { InventoryPage } from "./pages/InventoryPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { RemediationNewPage } from "./pages/RemediationNewPage";
import { RemediationPage } from "./pages/RemediationPage";
import { RemediationPlanPage } from "./pages/RemediationPlanPage";
import { SignInPage } from "./pages/SignInPage";

/** Sections that are implemented; the rest render a placeholder. */
const pages: Partial<Record<string, ReactNode>> = {
  "/": <DashboardPage />,
  "/inventory": <InventoryPage />,
  "/findings": <FindingsPage />,
  "/evidence": <EvidencePage />,
  "/exceptions": <ExceptionsPage />,
  "/remediation": <RemediationPage />,
};

export const routes: RouteObject[] = [
  {
    element: (
      <AuthProvider>
        <Layout />
      </AuthProvider>
    ),
    children: [
      { path: "/sign-in", element: <SignInPage /> },
      {
        element: <RequireSession />,
        children: [
          ...navItems.map((item): RouteObject => ({
            path: item.path,
            element: pages[item.path] ?? (
              <PlaceholderPage title={item.label} description={item.description} />
            ),
          })),
          { path: "/findings/:findingId", element: <FindingDetailPage /> },
          { path: "/exceptions/:exceptionId", element: <ExceptionDetailPage /> },
          { path: "/remediation/new", element: <RemediationNewPage /> },
          { path: "/remediation/:planId", element: <RemediationPlanPage /> },
        ],
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
