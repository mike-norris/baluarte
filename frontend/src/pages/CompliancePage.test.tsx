import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ControlException, Posture } from "../api/compliance";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { catalogue, control, controlException, posture } from "../test/compliance";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["audit_log:read", "compliance:read", "evidence:read", "findings:read"],
};

const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "audit_log:read", "compliance:read", "settings:manage"],
  csrf_token: "a".repeat(64),
};

const POSTURE = "GET /api/v1/compliance/posture";
const EXCEPTIONS = "GET /api/v1/compliance/exceptions";

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(
  user: CurrentUser,
  current: () => Posture = () => posture(),
  exceptions: () => ControlException[] = () => [],
  extra: Routes = {},
): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [POSTURE]: () => json(current()),
    "GET /api/v1/compliance/catalogue": () => json(catalogue),
    [EXCEPTIONS]: () => json(exceptions()),
    ...extra,
  });
}

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function bodyOf(calls: Call[], method: string, url: string): unknown {
  const call = calls.filter((c) => c.url === url && c.init?.method === method).at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("compliance page", () => {
  it("shows auditors the posture, problems first, with the selected framework's references", async () => {
    api(auditor);
    renderAt("/compliance");

    const list = await screen.findByRole("list", { name: "Controls" });
    const items = within(list).getAllByRole("listitem");
    expect(items.map((item) => within(item).getByRole("heading").textContent)).toEqual([
      "The web address uses TLS",
      "Backups are encrypted and tested",
      "Idle sessions end",
    ]);
    expect(items[0]).toHaveTextContent("Not met");
    expect(items[0]).toHaveTextContent("SOC 2 CC6.7");
    expect(items[0]).not.toHaveTextContent("ISO 27001");
    expect(items[0]).toHaveTextContent("The public URL is http://127.0.0.1:8090.");
    expect(items[1]).toHaveTextContent("Needs attestation");

    expect(screen.getByLabelText("Controls by status")).toHaveTextContent("Not met1");
    expect(screen.getByText(/Frameworks: SOC 2 Type II\./)).toBeInTheDocument();
    // Read-only for auditors, and the banner is not repeated on this page.
    expect(screen.queryByRole("button", { name: /Attest/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request an exception" })).not.toBeInTheDocument();
    expect(
      screen.queryByText(/does not fully meet its compliance profile/),
    ).not.toBeInTheDocument();
  });

  it("explains how to fix a control and shows its evidence", async () => {
    api(auditor);
    const user = userEvent.setup();
    renderAt("/compliance");

    const [first] = await screen.findAllByText(/Details and how to fix/, {
      selector: "summary",
    });
    if (!first) throw new Error("missing details");
    await user.click(first);
    expect(screen.getByText("Put Baluarte behind a TLS-terminating proxy.")).toBeVisible();
    expect(screen.getByText("public url")).toBeVisible();
  });

  it("lets an administrator attest an attestation control", async () => {
    let attested = false;
    const calls = api(
      admin,
      () =>
        attested
          ? posture({
              controls: [
                control({
                  id: "backups",
                  attestation: {
                    id: 3,
                    attested_by: "user:1",
                    attested_at: "2026-11-02T12:00:00Z",
                    expires_at: "2027-01-31T12:00:00Z",
                  },
                }),
              ],
            })
          : posture(),
      () => [],
      {
        "POST /api/v1/compliance/attestations": () => {
          attested = true;
          return json(
            {
              id: 3,
              control_id: "backups",
              statement: "Nightly encrypted dumps to S3; restore tested 2026-10-15.",
              attested_by: "user:1",
              attested_at: "2026-11-02T12:00:00Z",
              expires_at: "2027-01-31T12:00:00Z",
            },
            201,
          );
        },
      },
    );
    const user = userEvent.setup();
    renderAt("/compliance");

    const list = await screen.findByRole("list", { name: "Controls" });
    const backups = within(list).getAllByRole("listitem")[1];
    if (!backups) throw new Error("missing control");
    await user.click(within(backups).getByText(/Details and how to fix/));
    await user.click(within(backups).getByRole("button", { name: /Attest/ }));
    await user.click(screen.getByRole("button", { name: "Record attestation" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Write a statement of 10 to 2,000");

    await user.type(
      screen.getByRole("textbox", { name: "Statement" }),
      "Nightly encrypted dumps to S3; restore tested 2026-10-15.",
    );
    await user.click(screen.getByRole("button", { name: "Record attestation" }));

    expect(await screen.findByText(/Attested "Backups are encrypted and tested"/)).toBeVisible();
    expect(bodyOf(calls, "POST", "/api/v1/compliance/attestations")).toEqual({
      control_id: "backups",
      statement: "Nightly encrypted dumps to S3; restore tested 2026-10-15.",
    });
    expect(await screen.findByText(/Attested by user:1/)).toBeInTheDocument();
  });

  it("prompts an administrator to choose a profile when none is chosen", async () => {
    api(admin, () => posture({ configured: false, frameworks: [] }));
    renderAt("/compliance");

    expect(await screen.findByText("No compliance profile has been chosen.")).toBeVisible();
    const prompt = screen.getByRole("alert");
    expect(within(prompt).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/settings#compliance",
    );
    expect(screen.getByText(/Frameworks: None \(standard defaults\)/)).toBeInTheDocument();
  });

  it("tells an auditor to ask an administrator when no profile is chosen", async () => {
    api(auditor, () => posture({ configured: false, frameworks: [] }));
    renderAt("/compliance");

    expect(await screen.findByText(/Ask a Baluarte administrator/)).toBeVisible();
  });
});

describe("compliance exceptions", () => {
  it("lets another administrator approve or reject, but not the requester", async () => {
    let approved = false;
    const calls = api(
      admin,
      () => posture(),
      () =>
        approved
          ? [
              controlException({
                state: "approved",
                in_force: true,
                decided_by: "user:1",
              }),
            ]
          : [controlException(), controlException({ id: 8, requested_by: "user:1" })],
      {
        "POST /api/v1/compliance/exceptions/7/approve": () => {
          approved = true;
          return json(controlException({ state: "approved", in_force: true }));
        },
      },
    );
    const user = userEvent.setup();
    renderAt("/compliance");

    const table = await screen.findByRole("table", {
      name: "Exceptions to the compliance profile",
    });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("The web address uses TLS");
    expect(rows[1]).toHaveTextContent("Awaiting approval");
    expect(rows[2]).toHaveTextContent("Another administrator must decide.");
    expect(within(rows[2] as HTMLElement).queryByRole("button")).not.toBeInTheDocument();

    await user.click(within(rows[1] as HTMLElement).getByRole("button", { name: /Approve/ }));

    expect(await screen.findByText("Exception 7 approved.")).toBeVisible();
    expect(bodyOf(calls, "POST", "/api/v1/compliance/exceptions/7/approve")).toEqual({
      note: null,
    });
    // The list reloads after the decision.
    const reloaded = await screen.findByRole("table", {
      name: "Exceptions to the compliance profile",
    });
    expect(await within(reloaded).findByText("In force")).toBeInTheDocument();
  });

  it("requires a note to reject an exception", async () => {
    const calls = api(admin, () => posture(), () => [controlException()], {
      "POST /api/v1/compliance/exceptions/7/reject": () =>
        json(controlException({ state: "rejected" })),
    });
    const user = userEvent.setup();
    renderAt("/compliance");

    const table = await screen.findByRole("table", {
      name: "Exceptions to the compliance profile",
    });
    const row = within(table).getAllByRole("row")[1] as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /Reject/ }));
    expect(await within(row).findByText("Explain why in the note before rejecting.")).toBeVisible();

    await user.type(within(row).getByLabelText("Note (required to reject)"), "Use TLS instead");
    await user.click(within(row).getByRole("button", { name: /Reject/ }));

    expect(await screen.findByText("Exception 7 rejected.")).toBeVisible();
    expect(bodyOf(calls, "POST", "/api/v1/compliance/exceptions/7/reject")).toEqual({
      note: "Use TLS instead",
    });
  });

  it("revokes an exception in force after confirmation", async () => {
    const calls = api(
      admin,
      () => posture(),
      () => [controlException({ state: "approved", in_force: true })],
      {
        "POST /api/v1/compliance/exceptions/7/revoke": () => json(controlException()),
      },
    );
    const user = userEvent.setup();
    renderAt("/compliance");

    await user.click(await screen.findByRole("button", { name: /Revoke/ }));
    expect(calls.some((call) => call.url.endsWith("/revoke"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Confirm revoke" }));

    expect(await screen.findByText("Exception 7 revoked.")).toBeVisible();
  });

  it("requests an exception with a justification and an expiry within a year", async () => {
    vi.setSystemTime(new Date("2026-11-02T12:00:00Z"));
    const calls = api(
      admin,
      () => posture(),
      () => [],
      {
        "POST /api/v1/compliance/exceptions": () => json(controlException(), 201),
      },
    );
    const user = userEvent.setup();
    renderAt("/compliance");

    await user.click(await screen.findByRole("button", { name: "Request an exception" }));
    const form = screen.getByRole("form", { name: "Request an exception" });
    await user.selectOptions(within(form).getByRole("combobox", { name: "Control" }), "tls-web");
    const expiry = within(form).getByLabelText("Expires on");
    await user.clear(expiry);
    await user.type(expiry, "2099-01-01");
    await user.click(within(form).getByRole("button", { name: "Request exception" }));

    const alert = within(form).getByRole("alert");
    expect(alert).toHaveTextContent("Give a justification of 10 to 2,000 characters.");
    expect(alert).toHaveTextContent("Choose an expiry date from");

    await user.type(
      within(form).getByRole("textbox", { name: "Justification" }),
      "Local UAT only; TLS terminates at the proxy in production.",
    );
    await user.clear(expiry);
    await user.type(expiry, "2026-12-31");
    await user.click(within(form).getByRole("button", { name: "Request exception" }));

    expect(
      await screen.findByText(/waiting for another administrator to approve it/),
    ).toBeVisible();
    expect(bodyOf(calls, "POST", "/api/v1/compliance/exceptions")).toEqual({
      control_id: "tls-web",
      justification: "Local UAT only; TLS terminates at the proxy in production.",
      expires_on: "2026-12-31",
    });
  });
});

describe("compliance banner", () => {
  it("tells auditors and administrators on other pages when the profile is not met", async () => {
    api(
      auditor,
      () => posture(),
      () => [],
      {
        "GET /api/v1/audit-events/actions": () => json([]),
        "GET /api/v1/audit-events?limit=50": () => json({ items: [], next_before_id: null }),
      },
    );
    renderAt("/audit-log");

    const banner = await screen.findByText(/does not fully meet its compliance profile/);
    expect(banner).toHaveTextContent("(SOC 2 Type II): 1 not met, 1 awaiting attestation.");
    expect(screen.getByRole("link", { name: "Review compliance" })).toHaveAttribute(
      "href",
      "/compliance",
    );
  });

  it("stays hidden before a profile is chosen and when everything is met", async () => {
    for (const current of [
      posture({ configured: false, frameworks: [] }),
      posture({
        summary: {
          met: 2,
          not_met: 0,
          needs_attestation: 0,
          excepted: 1,
          not_applicable: 0,
        },
      }),
    ]) {
      vi.unstubAllGlobals();
      const calls = api(
        auditor,
        () => current,
        () => [],
        {
          "GET /api/v1/audit-events/actions": () => json([]),
          "GET /api/v1/audit-events?limit=50": () => json({ items: [], next_before_id: null }),
        },
      );
      const { unmount } = render(
        <RouterProvider
          router={createMemoryRouter(routes, {
            initialEntries: ["/audit-log"],
          })}
        />,
      );
      await screen.findByRole("heading", { level: 1, name: "Audit log" });
      await vi.waitFor(() => {
        expect(calls.some((call) => call.url === "/api/v1/compliance/posture")).toBe(true);
      });
      expect(screen.queryByText(/does not fully meet/)).not.toBeInTheDocument();
      unmount();
    }
  });

  it("is not requested for users without compliance:read", async () => {
    const calls = mockApi({
      "GET /api/v1/auth/me": () => json(analyst),
      "GET /api/v1/gitlab/freshness": () => json(freshness()),
      [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    });
    renderAt("/governance/changes");
    await screen.findByRole("navigation", { name: "Primary" });
    expect(calls.some((call) => call.url.includes("/compliance/"))).toBe(false);
  });
});
