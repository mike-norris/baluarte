import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import { Navigate, useLocation, useParams, type RouteObject } from "react-router";

import { AuthProvider } from "./auth/AuthProvider";
import { withStaleChunkReload } from "./chunkReload";
import { RequireSession } from "./auth/RequireSession";
import { Layout } from "./components/Layout";
import { navTitle, sectionItems } from "./navigation";
import { DashboardPage } from "./pages/DashboardPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { RouteErrorPage } from "./pages/RouteErrorPage";
import { SetupPage } from "./pages/SetupPage";
import { SignInPage } from "./pages/SignInPage";
import {
  APPLY_VIEW,
  baselineHref,
  CHANGES_VIEW,
  GOVERNANCE,
  SECURITY_POLICIES_VIEW,
  TEMPLATES_VIEW,
} from "./policies/format";

/** A page loaded on first visit, so the first screen doesn't download every page. */
function page<Name extends string>(
  load: () => Promise<Record<Name, ComponentType>>,
  name: Name,
): ReactNode {
  const loadCurrent = withStaleChunkReload(load);
  const Page = lazy(async (): Promise<{ default: ComponentType }> => ({
    default: (await loadCurrent())[name],
  }));
  return (
    <Suspense
      fallback={
        <p role="status" className="text-secondary">
          Loading…
        </p>
      }
    >
      <Page />
    </Suspense>
  );
}

/** Old Policies links keep working: they open the same view under Governance. */
function OldBaselineLink() {
  const { name = "" } = useParams();
  const { search } = useLocation();
  const version = Number(new URLSearchParams(search).get("version"));
  return (
    <Navigate
      to={baselineHref(name, Number.isInteger(version) && version > 0 ? version : undefined)}
      replace
    />
  );
}

function OldEditorLink() {
  const { search } = useLocation();
  return <Navigate to={`${GOVERNANCE}/baselines/new${search}`} replace />;
}

/** Sections that are implemented; the rest render a placeholder. */
const pages: Partial<Record<string, ReactNode>> = {
  "/": <DashboardPage />,
  "/reviews": page(() => import("./pages/ReviewsPage"), "ReviewsPage"),
  "/inventory": page(() => import("./pages/InventoryPage"), "InventoryPage"),
  "/findings": page(() => import("./pages/FindingsPage"), "FindingsPage"),
  "/changes": page(() => import("./pages/ChangesPage"), "ChangesPage"),
  "/labels": page(() => import("./pages/LabelsPage"), "LabelsPage"),
  "/manager": page(() => import("./pages/ManagerPage"), "ManagerPage"),
  "/evidence": page(() => import("./pages/EvidencePage"), "EvidencePage"),
  "/controls": page(() => import("./pages/ControlsPage"), "ControlsPage"),
  "/exceptions": page(() => import("./pages/ExceptionsPage"), "ExceptionsPage"),
  "/remediation": page(() => import("./pages/RemediationPage"), "RemediationPage"),
  [GOVERNANCE]: page(() => import("./pages/BaselinesPage"), "BaselinesPage"),
  "/compliance": page(() => import("./pages/CompliancePage"), "CompliancePage"),
  "/audit-log": page(() => import("./pages/AuditLogPage"), "AuditLogPage"),
  "/register": page(() => import("./pages/RegisterPage"), "RegisterPage"),
  "/workspaces": page(() => import("./pages/WorkspacesPage"), "WorkspacesPage"),
  "/connections": page(() => import("./pages/ConnectionsPage"), "ConnectionsPage"),
  "/settings": page(() => import("./pages/SettingsPage"), "SettingsPage"),
  "/help": page(() => import("./pages/HelpPage"), "HelpPage"),
};

export const routes: RouteObject[] = [
  {
    element: (
      <AuthProvider>
        <Layout />
      </AuthProvider>
    ),
    errorElement: <RouteErrorPage />,
    children: [
      { path: "/sign-in", element: <SignInPage /> },
      { path: "/setup", element: <SetupPage /> },
      {
        element: <RequireSession />,
        children: [
          ...sectionItems.map((item): RouteObject => ({
            path: item.path,
            element: pages[item.path] ?? (
              <PlaceholderPage title={navTitle(item)} description={item.description} />
            ),
          })),
          {
            path: "/inventory/projects/:gitlabId",
            element: page(() => import("./pages/InventoryItemPage"), "ProjectPage"),
          },
          {
            path: "/inventory/groups/:gitlabId",
            element: page(() => import("./pages/InventoryItemPage"), "GroupPage"),
          },
          {
            path: "/findings/:findingId",
            element: page(() => import("./pages/FindingDetailPage"), "FindingDetailPage"),
          },
          {
            path: "/changes/:changeId",
            element: page(() => import("./pages/ChangeDetailPage"), "ChangeDetailPage"),
          },
          {
            path: "/register/issues/:projectId/:iid",
            element: page(() => import("./pages/RegisterIssuePage"), "RegisterIssuePage"),
          },
          {
            path: "/register/controls",
            element: page(() => import("./pages/RegisterControlsPage"), "RegisterControlsPage"),
          },
          {
            path: "/register/rules",
            element: page(() => import("./pages/RegisterPage"), "RegisterRulesPage"),
          },
          {
            path: "/manager/trends",
            element: page(() => import("./pages/ManagerPage"), "TrendsPage"),
          },
          {
            path: "/manager/time",
            element: page(() => import("./pages/TimePage"), "TimePage"),
          },
          {
            path: "/manager/timecard",
            element: page(() => import("./pages/TimecardPage"), "TimecardPage"),
          },
          {
            path: "/manager/tags",
            element: page(() => import("./pages/TagsPage"), "TagsPage"),
          },
          {
            path: "/manager/tags/:tagId",
            element: page(() => import("./pages/TagsPage"), "TagPage"),
          },
          {
            path: "/labels/owners/:ownerType/:gitlabId",
            element: page(() => import("./pages/LabelOwnerPage"), "LabelOwnerPage"),
          },
          {
            path: "/labels/taxonomy/new",
            element: page(() => import("./pages/TaxonomyEditorPage"), "TaxonomyEditorPage"),
          },
          {
            path: "/labels/plans",
            element: page(() => import("./pages/LabelPlansPage"), "LabelPlansPage"),
          },
          {
            path: "/labels/plans/:planId",
            element: page(() => import("./pages/LabelPlanPage"), "LabelPlanPage"),
          },
          {
            path: "/time/issue-plans",
            element: page(() => import("./pages/IssuePlansPage"), "IssuePlansPage"),
          },
          {
            path: "/time/issue-plans/:planId",
            element: page(() => import("./pages/IssuePlanPage"), "IssuePlanPage"),
          },
          {
            path: "/controls/:framework/:controlId",
            element: page(() => import("./pages/ControlDetailPage"), "ControlDetailPage"),
          },
          {
            path: "/exceptions/:exceptionId",
            element: page(() => import("./pages/ExceptionDetailPage"), "ExceptionDetailPage"),
          },
          {
            path: "/help/:topic",
            element: page(() => import("./pages/HelpPage"), "HelpTopicPage"),
          },
          {
            path: "/remediation/new",
            element: page(() => import("./pages/RemediationNewPage"), "RemediationNewPage"),
          },
          {
            path: "/remediation/:planId",
            element: page(() => import("./pages/RemediationPlanPage"), "RemediationPlanPage"),
          },
          {
            path: APPLY_VIEW,
            element: page(
              () => import("./pages/WhereBaselinesApplyPage"),
              "WhereBaselinesApplyPage",
            ),
          },
          {
            path: SECURITY_POLICIES_VIEW,
            element: page(
              () => import("./pages/GitLabSecurityPoliciesPage"),
              "GitLabSecurityPoliciesPage",
            ),
          },
          {
            path: `${SECURITY_POLICIES_VIEW}/coverage`,
            element: page(
              () => import("./pages/SecurityPolicyCoveragePage"),
              "SecurityPolicyCoveragePage",
            ),
          },
          {
            path: `${SECURITY_POLICIES_VIEW}/:policyId`,
            element: page(() => import("./pages/SecurityPolicyPage"), "SecurityPolicyPage"),
          },
          {
            path: TEMPLATES_VIEW,
            element: page(() => import("./pages/TemplatesPage"), "TemplatesPage"),
          },
          {
            path: CHANGES_VIEW,
            element: page(() => import("./pages/PolicyChangesPage"), "PolicyChangesPage"),
          },
          {
            path: `${GOVERNANCE}/baselines/new`,
            element: page(() => import("./pages/BaselineEditorPage"), "BaselineEditorPage"),
          },
          {
            path: `${GOVERNANCE}/baselines/:name`,
            element: page(() => import("./pages/BaselinePage"), "BaselinePage"),
          },
          { path: "/policies", element: <Navigate to={GOVERNANCE} replace /> },
          { path: "/policies/:name", element: <OldBaselineLink /> },
          { path: "/policy-versions/new", element: <OldEditorLink /> },
        ],
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
