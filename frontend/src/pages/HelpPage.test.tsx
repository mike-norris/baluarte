import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, RouterProvider, createMemoryRouter, matchRoutes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { helpTopicFor, helpTopics } from "../help/topics";
import { navItems } from "../navigation";
import { routes } from "../routes";
import { signedIn } from "../test/api";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

describe("help", () => {
  beforeEach(() => {
    signedIn();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists every topic on the front page", async () => {
    renderAt("/help");

    expect(await screen.findByRole("heading", { level: 1, name: "Help" })).toBeInTheDocument();
    const cards = screen.getAllByRole("heading", { level: 3 });
    expect(cards.map((heading) => heading.textContent)).toEqual(
      expect.arrayContaining(helpTopics.map((topic) => topic.title)),
    );
    for (const topic of helpTopics) {
      const heading = cards.find((card) => card.textContent === topic.title);
      expect(within(heading as HTMLElement).getByRole("link")).toHaveAttribute(
        "href",
        `/help/${topic.slug}`,
      );
    }
  });

  it("shows the GitLab gaps topic with what each gap needs and where Baluarte covers it", async () => {
    renderAt("/help/gitlab-gaps");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Gaps in GitLab that Baluarte covers" }),
    ).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText(/Audit events need Premium or Ultimate/)).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Changes" })).toHaveAttribute(
      "href",
      "/changes",
    );
    expect(screen.getByText(/it does not block a push or a merge/)).toBeInTheDocument();
  });

  it("moves between topics in reading order", async () => {
    const user = userEvent.setup();
    const router = renderAt("/help/daily-work");

    const more = await screen.findByRole("navigation", { name: "More help" });
    await user.click(within(more).getByRole("link", { name: /Gaps in GitLab/ }));

    expect(router.state.location.pathname).toBe("/help/gitlab-gaps");
  });

  it("shows a not-found page for an unknown topic", async () => {
    renderAt("/help/no-such-topic");

    expect(
      await screen.findByRole("heading", { level: 1, name: "Page not found" }),
    ).toBeInTheDocument();
  });

  it("links the header's help button to the topic for the current page", async () => {
    renderAt("/findings");

    expect(await screen.findByRole("link", { name: "Help: Findings" })).toHaveAttribute(
      "href",
      "/help/findings",
    );
  });
});

describe("help topics", () => {
  it("explain every page in the navigation", () => {
    const unexplained = navItems
      .filter((item) => item.path !== "/help" && !helpTopicFor(item.path))
      .map((item) => item.path);
    expect(unexplained).toEqual([]);
  });

  it("match a page to its topic by the longest path", () => {
    expect(helpTopicFor("/")?.slug).toBe("getting-started");
    expect(helpTopicFor("/labels/plans/3")?.slug).toBe("labels");
    expect(helpTopicFor("/manager")?.slug).toBe("manager");
    expect(helpTopicFor("/manager/time")?.slug).toBe("time");
    expect(helpTopicFor("/managers")).toBeUndefined();
    expect(helpTopicFor("/governance/baselines/core")?.slug).toBe("baselines");
  });

  it.each(helpTopics)("$slug links only to pages that exist", (topic) => {
    render(<MemoryRouter>{topic.body}</MemoryRouter>);

    for (const link of screen.queryAllByRole("link")) {
      const href = link.getAttribute("href") ?? "";
      const matched = matchRoutes(routes, href) ?? [];
      expect(matched.length, href).toBeGreaterThan(0);
      expect(matched[matched.length - 1]?.route.path, href).not.toBe("*");
      if (href.startsWith("/help/")) {
        expect(helpTopics.map((other) => `/help/${other.slug}`)).toContain(href);
      }
    }
  });
});
