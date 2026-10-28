import type { RouteObject } from "react-router";

import { Layout } from "./components/Layout";
import { navItems } from "./navigation";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      ...navItems.map(
        (item): RouteObject => ({
          path: item.path,
          element: <PlaceholderPage title={item.label} description={item.description} />,
        }),
      ),
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
