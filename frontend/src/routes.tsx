import type { ReactNode } from "react";
import type { RouteObject } from "react-router";

import { AuthProvider } from "./auth/AuthProvider";
import { RequireSession } from "./auth/RequireSession";
import { Layout } from "./components/Layout";
import { CollectionNote } from "./freshness/CollectionNote";
import { navItems } from "./navigation";
import { DashboardPage } from "./pages/DashboardPage";
import { InventoryPage } from "./pages/InventoryPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { SignInPage } from "./pages/SignInPage";

/** Sections that are implemented; the rest render a placeholder. */
const pages: Partial<Record<string, ReactNode>> = {
  "/": <DashboardPage />,
  "/inventory": <InventoryPage />,
};

/** Placeholders whose content will be derived from GitLab data, so they show its freshness. */
const collectionDerived = new Set(["/findings", "/evidence"]);

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
        children: navItems.map((item): RouteObject => ({
          path: item.path,
          element: pages[item.path] ?? (
            <PlaceholderPage title={item.label} description={item.description}>
              {collectionDerived.has(item.path) && <CollectionNote />}
            </PlaceholderPage>
          ),
        })),
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
