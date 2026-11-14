import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { Badge, badgeClass } from "./Badge";
import { DigestDisplay } from "./DigestDisplay";
import { Dialog } from "./Dialog";
import { Lifecycle } from "./Lifecycle";
import { PageHeader } from "./PageHeader";
import { Pagination } from "./Pagination";
import { ResponsiveTable } from "./ResponsiveTable";

const DIGEST = "3f9a1c0e7b2d55f0e1a98c4be2d7f0a91c3e6b5d7a4c21e";

describe("shared components", () => {
  it("uses subtle badges, and solid ones only where no subtle variant exists", () => {
    expect(badgeClass("success")).toBe(
      "badge bg-success-subtle text-success-emphasis border border-success-subtle",
    );
    expect(badgeClass("indigo")).toBe("badge bg-indigo text-indigo-fg");
    render(<Badge tone="danger">Fail</Badge>);
    expect(screen.getByText("Fail")).toHaveClass("bg-danger-subtle");
  });

  it("gives the page one level-1 heading", () => {
    render(<PageHeader title="Findings" pretitle="Observe" subtitle="Rule outcomes." />);
    expect(screen.getByRole("heading", { level: 1, name: "Findings" })).toHaveAttribute(
      "id",
      "page-title",
    );
  });

  it("labels each cell with its column, so rows stack readably on phones", () => {
    render(
      <ResponsiveTable
        caption="Findings"
        rows={[{ id: 1, rule: "protected-branch", project: "acme/web" }]}
        rowKey={(row) => row.id}
        columns={[
          { key: "rule", header: "Rule", render: (row) => row.rule },
          { key: "project", header: "Project", render: (row) => row.project, priority: 2 },
        ]}
      />,
    );
    const table = screen.getByRole("table", { name: "Findings" });
    const cells = within(table).getAllByRole("cell");
    expect(cells.map((cell) => cell.getAttribute("data-label"))).toEqual(["Rule", "Project"]);
    expect(cells[1]).toHaveClass("d-none", "d-md-table-cell");
  });

  it("marks the current lifecycle step and the ones done", () => {
    render(<Lifecycle states={["Draft", "Submitted", "Approved"]} current="Submitted" />);
    const steps = screen.getAllByRole("listitem");
    expect(steps[0]).toHaveTextContent("Draft (done)");
    expect(steps[1]).toHaveAttribute("aria-current", "step");
    expect(steps[2]).not.toHaveAttribute("aria-current");
  });

  it("abbreviates a digest and shows it in full on request", async () => {
    const user = userEvent.setup();
    render(<DigestDisplay value={DIGEST} />);
    expect(screen.getByText("3f9a1c0e7b2d…a4c21e")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show full digest" }));
    expect(screen.getByText(DIGEST)).toBeInTheDocument();
  });

  it("turns paging buttons off at the ends of a list", () => {
    render(
      <Pagination
        range="Showing 1–50"
        hasPrevious={false}
        hasNext
        onPrevious={vi.fn()}
        onNext={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("focuses a dialog's heading, closes on Escape and returns focus", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button
            type="button"
            onClick={() => {
              setOpen(true);
            }}
          >
            Approve…
          </button>
          <Dialog
            open={open}
            title="Approve this plan?"
            onClose={() => {
              setOpen(false);
            }}
            footer={<button type="button">Confirm approval</button>}
          >
            <p>12 steps.</p>
          </Dialog>
        </>
      );
    }
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Approve…" });
    await user.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Approve this plan?" });
    expect(within(dialog).getByRole("heading", { name: "Approve this plan?" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});
