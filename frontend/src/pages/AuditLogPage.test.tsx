import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuditEvent } from "../api/audit";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["audit_log:read", "evidence:export", "evidence:read", "findings:read"],
};

function event(id: number, overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id,
    occurred_at: "2026-11-01T10:00:00Z",
    actor: "user:1",
    action: "policy.version_created",
    resource_type: "policy",
    resource_id: `baseline@${String(id)}`,
    details: { name: "baseline", version: id },
    details_digest: "b".repeat(64),
    digest_valid: true,
    ...overrides,
  };
}

function api(user: CurrentUser, extra: Record<string, () => Response> = {}) {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    "GET /api/v1/audit-events/actions": () =>
      json(["auth.login", "policy.version_created", "settings.governance_updated"]),
    "GET /api/v1/audit-events?limit=50": () =>
      json({ items: [event(3), event(2, { digest_valid: false })], next_before_id: 2 }),
    "GET /api/v1/audit-events?before_id=2&limit=50": () =>
      json({ items: [event(1, { action: "auth.login" })], next_before_id: null }),
    ...extra,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("audit log", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists events newest first, flags a digest that no longer matches, and pages", async () => {
    api(auditor);
    const user = userEvent.setup();
    renderAt("/audit-log");

    const table = await screen.findByRole("table", { name: "Audit events, newest first" });
    let rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("policy.version_created");
    expect(rows[1]).toHaveTextContent("policy baseline@3");
    expect(rows[1]).toHaveTextContent("name: baseline, version: 3");
    expect(rows[1]).toHaveTextContent("Verifies");
    expect(rows[2]).toHaveTextContent("Does not match");
    expect(screen.getByRole("alert")).toHaveTextContent("1 event's details no longer match");
    expect(screen.getByText("baluarte.audit")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load older events" }));

    rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(4);
    expect(rows[3]).toHaveTextContent("auth.login");
    expect(screen.queryByRole("button", { name: "Load older events" })).not.toBeInTheDocument();
  });

  it("filters by action, actor and UTC dates", async () => {
    const calls = api(auditor, {
      "GET /api/v1/audit-events?action=auth.login&actor=user%3A7&since=2026-10-01T00%3A00%3A00Z&until=2026-11-01T00%3A00%3A00.000Z&limit=50":
        () =>
          json({
            items: [event(9, { action: "auth.login", actor: "user:7" })],
            next_before_id: null,
          }),
    });
    const user = userEvent.setup();
    const router = renderAt("/audit-log");

    const form = await screen.findByRole("form", { name: "Filter the audit log" });
    await within(form).findByRole("option", { name: "auth.login" });
    await user.selectOptions(within(form).getByLabelText("Action"), "auth.login");
    await user.type(within(form).getByLabelText("Actor"), "user:7");
    await user.type(within(form).getByLabelText("From (UTC)"), "2026-10-01");
    await user.type(within(form).getByLabelText("To (UTC, inclusive)"), "2026-10-31");
    await user.click(within(form).getByRole("button", { name: "Apply" }));

    expect(router.state.location.search).toBe(
      "?action=auth.login&actor=user%3A7&from=2026-10-01&to=2026-10-31",
    );
    const table = await screen.findByRole("table", { name: "Audit events, newest first" });
    expect(await within(table).findByText("user:7")).toBeInTheDocument();
    expect(calls.some((call) => call.url.includes("action=auth.login"))).toBe(true);
  });

  it("is not in the navigation for analysts", async () => {
    api(analyst);
    renderAt("/");

    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(nav).queryByRole("link", { name: "Audit log" })).not.toBeInTheDocument();
  });

  it("is in the navigation for auditors", async () => {
    api(auditor);
    renderAt("/");

    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(nav).getByRole("link", { name: "Audit log" })).toHaveAttribute(
      "href",
      "/audit-log",
    );
  });
});
