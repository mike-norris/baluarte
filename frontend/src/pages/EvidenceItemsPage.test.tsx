import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EvidenceItem } from "../api/evidenceItems";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";

const ME = "GET /api/v1/auth/me";
const LIST = "GET /api/v1/evidence-items";

const manager: CurrentUser = {
  ...analyst,
  permissions: [...analyst.permissions, "evidence:manage"],
};
const reviewer: CurrentUser = {
  ...analyst,
  user_id: 2,
  roles: ["auditor"],
  permissions: [...analyst.permissions, "evidence:manage", "evidence:review"],
};

const item: EvidenceItem = {
  id: 5,
  kind: "file",
  title: "Change board minutes",
  description: "",
  owner: { user_id: 1, display_name: "Ada" },
  period_start: null,
  period_end: null,
  valid_until: "2027-01-01",
  freshness: "fresh",
  restricted: false,
  state: "draft",
  controls: [{ framework: "soc2", version: "2017-r2022", control_id: "CC8.1" }],
  supersedes_id: null,
  superseded_by_id: null,
  filename: null,
  content_type: null,
  size_bytes: null,
  sha256: null,
  file_removed_at: null,
  url: null,
  check_status: null,
  checked_at: null,
  check_http_status: null,
  check_sha256: null,
  added_by: { user_id: 1, display_name: "Ada" },
  added_at: "2026-11-27T10:00:00Z",
  submitted_at: null,
  reviewed_by: null,
  reviewed_at: null,
  review_note: null,
  development_review: false,
};

function api(user: CurrentUser, routes: Record<string, (init?: RequestInit) => Response> = {}) {
  return mockApi({
    [ME]: () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    [LIST]: () => json({ items: [item], checks_enabled: false }),
    ...routes,
  });
}

function renderPage() {
  const router = createMemoryRouter(routes, { initialEntries: ["/evidence-items"] });
  render(<RouterProvider router={router} />);
}

function bodyOf(calls: Call[], key: string): unknown {
  const call = calls.find((c) => `${c.init?.method ?? "GET"} ${c.url}` === key);
  return JSON.parse(String(call?.init?.body));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EvidenceItemsPage", () => {
  it("adds a file item with a control, uploads its file and submits it", async () => {
    const user = userEvent.setup();
    let current = item;
    const calls = api(manager, {
      "POST /api/v1/evidence-items": () => json(item, 201),
      "GET /api/v1/evidence-items/5": () => json(current),
      "PUT /api/v1/evidence-items/5/file?filename=minutes.pdf": () => {
        current = { ...item, filename: "minutes.pdf", sha256: "a".repeat(64) };
        return json(current);
      },
      "POST /api/v1/evidence-items/5/submit": () => {
        current = { ...current, state: "submitted" };
        return json(current);
      },
    });
    renderPage();

    const form = await screen.findByRole("form", { name: "Add evidence" });
    await user.type(within(form).getByLabelText("Title"), "Change board minutes");
    await user.type(within(form).getByLabelText("Control"), "CC8.1");
    await user.click(within(form).getByRole("button", { name: "Add control" }));
    await user.click(within(form).getByRole("button", { name: "Add as draft" }));
    expect(await screen.findByText("Added Change board minutes as a draft.")).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/evidence-items")).toMatchObject({
      kind: "file",
      title: "Change board minutes",
      controls: [{ framework: "soc2", control_id: "CC8.1" }],
    });

    const detail = await screen.findByRole("region", { name: "Change board minutes" });
    const file = new File(["%PDF-1.7"], "minutes.pdf", { type: "application/pdf" });
    await user.upload(within(detail).getByLabelText("File for Change board minutes"), file);
    expect(await screen.findByText("Uploaded minutes.pdf.")).toBeInTheDocument();
    const upload = calls.find((c) => c.init?.method === "PUT");
    expect(upload?.init?.headers).toMatchObject({
      "Content-Type": "application/octet-stream",
      "X-CSRF-Token": manager.csrf_token,
    });

    await user.click(await screen.findByRole("button", { name: "Submit for review" }));
    expect(await screen.findByText("Submitted for review.")).toBeInTheDocument();
  });

  it("lets a reviewer accept someone else's item but not their own", async () => {
    const user = userEvent.setup();
    const waiting: EvidenceItem = { ...item, state: "submitted", sha256: "a".repeat(64) };
    const calls = api(reviewer, {
      [LIST]: () => json({ items: [waiting], checks_enabled: false }),
      "GET /api/v1/evidence-items/5": () => json(waiting),
      "POST /api/v1/evidence-items/5/accept": () =>
        json({
          ...waiting,
          state: "accepted",
          reviewed_by: { user_id: 2, display_name: "Bea" },
          reviewed_at: "2026-11-27T11:00:00Z",
        }),
    });
    renderPage();

    const list = await screen.findByRole("table", { name: "Evidence items" });
    await user.click(within(list).getByRole("button", { name: "Open Change board minutes" }));
    const detail = await screen.findByRole("region", { name: "Change board minutes" });
    expect(within(detail).getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.type(within(detail).getByLabelText("Review note"), "Matches the CAB log.");
    await user.click(within(detail).getByRole("button", { name: "Accept" }));
    expect(await screen.findByText("Accepted.")).toBeInTheDocument();
    expect(bodyOf(calls, "POST /api/v1/evidence-items/5/accept")).toEqual({
      note: "Matches the CAB log.",
    });
  });

  it("tells a reviewer that someone else reviews their own item", async () => {
    const user = userEvent.setup();
    const own: EvidenceItem = {
      ...item,
      state: "submitted",
      added_by: { user_id: 2, display_name: "Bea" },
    };
    api(reviewer, {
      [LIST]: () => json({ items: [own], checks_enabled: false }),
      "GET /api/v1/evidence-items/5": () => json(own),
    });
    renderPage();

    const list = await screen.findByRole("table", { name: "Evidence items" });
    await user.click(within(list).getByRole("button", { name: "Open Change board minutes" }));
    expect(await screen.findByText("Someone else reviews it")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
  });

  it("offers no adding to those without evidence:manage", async () => {
    api(analyst);
    renderPage();

    await screen.findByRole("table", { name: "Evidence items" });
    expect(screen.queryByRole("form", { name: "Add evidence" })).not.toBeInTheDocument();
  });
});
