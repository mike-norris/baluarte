import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { validatePackage, type PackageForm } from "./package";

const PACKAGE = "POST /api/v1/evidence/packages";
const RECORD = "GET /api/v1/evidence/packages/pkg-1";

const auditor: CurrentUser = {
  ...analyst,
  roles: ["auditor"],
  permissions: ["evidence:read", "findings:read", "inventory:read", "evidence:package"],
};

let saved: string[] = [];

beforeEach(() => {
  saved = [];
  URL.createObjectURL = vi.fn(() => "blob:package");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push(this.download);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function api(
  routes: Record<string, (init?: RequestInit) => Response>,
  user: CurrentUser = auditor,
): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...routes,
  });
}

function renderAt(path = "/evidence") {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function zip(headers: Record<string, string> = {}): Response {
  return new Response("PK", {
    status: 200,
    headers: {
      "Content-Type": "application/zip",
      "Content-Disposition": 'attachment; filename="baluarte-package-20261105.zip"',
      "X-Baluarte-Export-Id": "pkg-1",
      ...headers,
    },
  });
}

function body(calls: Call[]): unknown {
  const call = calls.find((item) => item.url === "/api/v1/evidence/packages");
  return JSON.parse(String(call?.init?.body));
}

const blank: PackageForm = {
  from: "2026-07-01T00:00",
  to: "2026-10-01T00:00",
  framework: "",
  control: "",
  groups: "",
  size: "",
  seed: "",
};

describe("package request", () => {
  it("builds the request with a default sample and no seed", () => {
    expect(validatePackage(blank)).toEqual({
      request: {
        from: "2026-07-01T00:00:00Z",
        to: "2026-10-01T00:00:00Z",
        sample: { size: 25 },
      },
    });
  });

  it("keeps the scope, sample size and seed", () => {
    expect(
      validatePackage({
        ...blank,
        framework: "soc2",
        control: " CC8.1 ",
        groups: "acme/payments, acme/platform",
        size: "40",
        seed: "123456",
      }),
    ).toEqual({
      request: {
        from: "2026-07-01T00:00:00Z",
        to: "2026-10-01T00:00:00Z",
        framework: "soc2",
        control_id: "CC8.1",
        groups: ["acme/payments", "acme/platform"],
        sample: { size: 40, seed: 123456 },
      },
    });
  });

  it("explains every problem", () => {
    expect(
      validatePackage({
        ...blank,
        to: "2026-06-01T00:00",
        control: "CC8.1",
        size: "501",
        seed: "-4",
      }),
    ).toEqual({
      errors: [
        "The period must end after it starts.",
        "Choose the framework the control belongs to.",
        "The sample size must be a whole number from 1 to 500.",
        "The seed must be a whole number, or left empty to have one chosen.",
      ],
    });
    expect(validatePackage({ ...blank, from: "" })).toEqual({
      errors: ["Choose when the period starts."],
    });
  });
});

describe("audit package form", () => {
  it("creates a package and shows the seed used and the manifest digest", async () => {
    const user = userEvent.setup();
    const calls = api({
      [PACKAGE]: () => zip({ "X-Baluarte-Sample-Seed": "987654321" }),
      [RECORD]: () =>
        json({
          export_id: "pkg-1",
          seed: 987654321,
          manifest_sha256: "d".repeat(64),
          sample_size: 25,
          from: "2026-07-01T00:00:00Z",
          to: "2026-10-01T00:00:00Z",
        }),
    });
    renderAt();

    const form = await screen.findByRole("form", { name: "Create an audit package" });
    await user.type(within(form).getByLabelText("From (UTC)"), "2026-07-01T00:00");
    await user.type(within(form).getByLabelText("Up to (UTC)"), "2026-10-01T00:00");
    await user.type(within(form).getByLabelText("Group paths"), "acme/payments");
    await user.click(within(form).getByRole("button", { name: "Create package" }));

    expect(
      await screen.findByText(
        "Downloaded baluarte-package-20261105.zip. Verify it before relying on it.",
      ),
    ).toBeInTheDocument();
    expect(saved).toEqual(["baluarte-package-20261105.zip"]);
    expect(body(calls)).toEqual({
      from: "2026-07-01T00:00:00Z",
      to: "2026-10-01T00:00:00Z",
      groups: ["acme/payments"],
      sample: { size: 25 },
    });
    expect(screen.getByText("987654321")).toBeInTheDocument();
    expect(await screen.findByText("d".repeat(64))).toBeInTheDocument();
  });

  it("offers to check again while the manifest digest is not recorded", async () => {
    const user = userEvent.setup();
    let digest: string | null = null;
    api({
      [PACKAGE]: () => zip({ "X-Baluarte-Sample-Seed": "42" }),
      [RECORD]: () => json({ export_id: "pkg-1", seed: 42, manifest_sha256: digest }),
    });
    renderAt();

    const form = await screen.findByRole("form", { name: "Create an audit package" });
    await user.type(within(form).getByLabelText("From (UTC)"), "2026-07-01T00:00");
    await user.type(within(form).getByLabelText("Up to (UTC)"), "2026-10-01T00:00");
    await user.type(within(form).getByLabelText("Seed"), "42");
    await user.click(within(form).getByRole("button", { name: "Create package" }));

    expect(await screen.findByText(/Not recorded yet/)).toBeInTheDocument();
    digest = "e".repeat(64);
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("e".repeat(64))).toBeInTheDocument();
  });

  it("does not send a package the form says is invalid", async () => {
    const user = userEvent.setup();
    const calls = api({});
    renderAt();

    const form = await screen.findByRole("form", { name: "Create an audit package" });
    await user.click(within(form).getByRole("button", { name: "Create package" }));

    expect(await screen.findByText("The package was not started:")).toBeInTheDocument();
    expect(screen.getByText("Choose when the period starts.")).toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/v1/evidence/packages")).toBe(false);
  });

  it("shows the API's refusal", async () => {
    const user = userEvent.setup();
    api({ [PACKAGE]: () => json({ detail: "no changes recorded in the period" }, 409) });
    renderAt();

    const form = await screen.findByRole("form", { name: "Create an audit package" });
    await user.type(within(form).getByLabelText("From (UTC)"), "2026-07-01T00:00");
    await user.type(within(form).getByLabelText("Up to (UTC)"), "2026-10-01T00:00");
    await user.click(within(form).getByRole("button", { name: "Create package" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Nothing can be exported yet: no changes recorded in the period.",
    );
  });

  it("is not offered without the package permission", async () => {
    api({}, { ...auditor, permissions: ["evidence:read"] });
    renderAt();

    expect(await screen.findByRole("heading", { level: 1, name: "Evidence" })).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Create an audit package" })).not.toBeInTheDocument();
  });
});
