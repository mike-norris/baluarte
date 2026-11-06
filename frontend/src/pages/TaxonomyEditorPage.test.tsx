import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PolicyChange, PolicyChangePage } from "../api/policies";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { PENDING_MAPPING } from "../test/controls";
import { EVALUATION_FRESHNESS, evaluationFreshness, freshness } from "../test/freshness";
import { TAXONOMY, TAXONOMY_YAML, taxonomyVersion, taxonomyView } from "../test/labels";
import { SCOPE_PROPOSED, exclusions } from "../test/scope";

const VALIDATE = "POST /api/v1/labels/taxonomy/validate";
const PROPOSE = "POST /api/v1/labels/taxonomy";
const reviewer: CurrentUser = { ...analyst, user_id: 2, display_name: "Grace Hopper" };

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser, routes: Routes): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    "GET /api/v1/gitlab/freshness": () => json(freshness()),
    [EVALUATION_FRESHNESS]: () => json(evaluationFreshness()),
    ...routes,
  });
}

function renderAt(path: string) {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

function bodyOf(calls: Call[], url: string): Record<string, unknown> {
  const call = calls.filter((c) => c.url === url && c.init?.method === "POST").at(-1);
  return JSON.parse(String(call?.init?.body)) as Record<string, unknown>;
}

const valid = () =>
  json({ valid: true, errors: [], name: "acme-labels", spec: taxonomyVersion(1).spec });

describe("taxonomy editor", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("starts from the taxonomy in force and proposes the form as YAML", async () => {
    const calls = api(analyst, {
      [TAXONOMY]: () => json(taxonomyView()),
      [VALIDATE]: valid,
      [PROPOSE]: () => json(taxonomyVersion(2, { state: "proposed" }), 201),
    });
    const user = userEvent.setup();
    renderAt("/labels/taxonomy/new");

    expect(await screen.findByLabelText("Taxonomy name")).toHaveValue("acme-labels");
    expect(screen.getByRole("heading", { name: "Labels (2)" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add a label" }));
    const added = screen.getByRole("group", { name: "Label 3" });
    await user.type(within(added).getByLabelText("Name"), "team::platform");
    const hex = within(added).getByLabelText("Colour (#rrggbb)");
    await user.clear(hex);
    await user.type(hex, "#00AA00");
    expect(within(added).getByText("#00aa00")).toBeInTheDocument();
    await user.type(within(added).getByLabelText(/aliases/), "platform, plat");
    await user.type(screen.getByLabelText("Justification (optional)"), "Teams asked for one label.");
    expect(
      screen.getByText(/Review is required: someone other than you must approve this/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Propose the taxonomy" }));

    expect(await screen.findByText(/Proposed acme-labels v2/)).toBeInTheDocument();
    const sent = bodyOf(calls, "/api/v1/labels/taxonomy");
    expect(sent.justification).toBe("Teams asked for one label.");
    expect(sent.source_yaml).toContain(
      '    - name: "team::platform"\n      color: "#00aa00"\n      aliases: ["platform", "plat"]',
    );
    expect(bodyOf(calls, "/api/v1/labels/taxonomy/validate").source_yaml).toBe(sent.source_yaml);
    expect(screen.getByRole("link", { name: "Open the review queue" })).toBeInTheDocument();
  });

  it("lists the form's problems and sends nothing", async () => {
    const calls = api(analyst, { [TAXONOMY]: () => json(taxonomyView({ current: null, versions: [] })) });
    const user = userEvent.setup();
    renderAt("/labels/taxonomy/new");

    await user.click(await screen.findByRole("button", { name: "Propose the taxonomy" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Name the taxonomy");
    expect(alert).toHaveTextContent("Add at least one label.");
    expect(calls.some((call) => call.init?.method === "POST")).toBe(false);
  });

  it("checks YAML with the API and shows its errors", async () => {
    api(analyst, {
      [TAXONOMY]: () => json(taxonomyView()),
      [VALIDATE]: () =>
        json({
          valid: false,
          errors: [{ path: "spec.labels[0].color", message: "must be #rrggbb" }],
          spec: null,
        }),
    });
    const user = userEvent.setup();
    renderAt("/labels/taxonomy/new");

    await user.click(await screen.findByRole("button", { name: "YAML" }));
    const yaml = screen.getByLabelText("Taxonomy YAML");
    // The YAML is written from the form, so it is what the form would propose.
    expect((yaml as HTMLTextAreaElement).value).toContain('    - name: "type::bug"\n      color: "#1f75cb"');
    await user.click(screen.getByRole("button", { name: "Check the YAML" }));
    expect(await screen.findByText("spec.labels[0].color: must be #rrggbb")).toBeInTheDocument();
    // Invalid YAML can't go back to the form.
    await user.click(screen.getByRole("button", { name: "Form" }));
    expect(screen.getByLabelText("Taxonomy YAML")).toBeInTheDocument();
  });

  it("says the taxonomy is in force at once when review is off", async () => {
    api(analyst, {
      [TAXONOMY]: () => json(taxonomyView({ review_required: false })),
      [VALIDATE]: valid,
      [PROPOSE]: () => json(taxonomyVersion(2, { self_approved: true }), 201),
    });
    const user = userEvent.setup();
    renderAt("/labels/taxonomy/new");

    await user.click(await screen.findByRole("button", { name: "Propose the taxonomy" }));
    expect(
      await screen.findByText(/Review is off, so it is in force now and was recorded as self-approved/),
    ).toBeInTheDocument();
  });
});

const page: PolicyChangePage = { review_required: true, items: [] as PolicyChange[], next_before_id: null };

describe("taxonomy review", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function governance(user: CurrentUser, extra: Routes = {}): Call[] {
    return api(user, {
      "GET /api/v1/policy-changes?state=proposed&limit=50": () => json(page),
      "GET /api/v1/policy-changes?limit=50": () => json(page),
      [PENDING_MAPPING]: () => json({ items: [] }),
      [SCOPE_PROPOSED]: () => json(exclusions([])),
      [TAXONOMY]: () =>
        json(
          taxonomyView({
            versions: [
              taxonomyVersion(2, {
                state: "proposed",
                justification: "Teams asked for one label.",
                source_yaml: TAXONOMY_YAML.replace("#d9534f", "#c9302c"),
                decided_by: null,
                decided_at: null,
              }),
              taxonomyVersion(1),
            ],
          }),
        ),
      ...extra,
    });
  }

  it("shows the proposed taxonomy's diff and lets someone else approve it", async () => {
    const calls = governance(reviewer, {
      "POST /api/v1/labels/taxonomy/2/approve": () => json(taxonomyVersion(2)),
    });
    const user = userEvent.setup();
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", { name: "Label taxonomies waiting for review" });
    expect(list).toHaveTextContent("Label taxonomy acme-labels v2, 2 labels");
    expect(list).toHaveTextContent("Teams asked for one label.");
    const diff = within(list).getByLabelText("Changes from the taxonomy in force");
    expect(diff).toHaveTextContent('- color: "#d9534f"');
    expect(diff).toHaveTextContent('+ color: "#c9302c"');
    await user.click(within(list).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/Approved: Label taxonomy acme-labels v2/)).toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/v1/labels/taxonomy/2/approve")).toBe(true);
  });

  it("does not offer the proposer a decision, and needs a note to reject", async () => {
    governance(analyst);
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", { name: "Label taxonomies waiting for review" });
    expect(list).toHaveTextContent("You proposed this taxonomy, so someone else must approve or reject it.");
    expect(within(list).queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("refuses a rejection without a note", async () => {
    const calls = governance(reviewer);
    const user = userEvent.setup();
    renderAt("/governance/changes");

    const list = await screen.findByRole("list", { name: "Label taxonomies waiting for review" });
    await user.click(within(list).getByRole("button", { name: "Reject" }));
    expect(within(list).getByRole("alert")).toHaveTextContent("Explain why in the note");
    expect(calls.some((call) => call.url.endsWith("/reject"))).toBe(false);
  });
});
