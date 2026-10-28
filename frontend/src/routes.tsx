import type { RouteObject } from "react-router";

import { AuthProvider } from "./auth/AuthProvider";
import { RequireSession } from "./auth/RequireSession";
import { Layout } from "./components/Layout";
import { navItems } from "./navigation";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { SignInPage } from "./pages/SignInPage";

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
        children: navItems.map(
          (item): RouteObject => ({
            path: item.path,
            element: <PlaceholderPage title={item.label} description={item.description} />,
          }),
        ),
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
