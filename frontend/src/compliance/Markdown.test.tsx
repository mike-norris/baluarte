import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown, parseMarkdown } from "./Markdown";

describe("how-to markdown", () => {
  it("splits headings, lists, paragraphs and fenced code", () => {
    expect(
      parseMarkdown(
        "## Start it\n\n1. Create a key\n2. Start the service\n\nThen check.\nIt runs daily.\n\n```sh\ndocker compose --profile backup up -d\n```\n- one\n- two",
      ),
    ).toEqual([
      { kind: "heading", text: "Start it" },
      { kind: "list", ordered: true, items: ["Create a key", "Start the service"] },
      { kind: "paragraph", text: "Then check. It runs daily." },
      { kind: "code", text: "docker compose --profile backup up -d" },
      { kind: "list", ordered: false, items: ["one", "two"] },
    ]);
  });

  it("renders text, never HTML, and links only safe addresses", () => {
    const { container } = render(
      <Markdown
        label="Example"
        source={
          "Run `age-keygen` and keep the **private key** offline. <img src=x onerror=alert(1)>\n\n" +
          "See [the guide](https://example.com/guide), [settings](/settings) and [bad](javascript:alert(1))."
        }
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(screen.getByText("age-keygen").tagName).toBe("CODE");
    expect(screen.getByText("private key").tagName).toBe("STRONG");
    expect(screen.getByRole("link", { name: "the guide" })).toHaveAttribute(
      "href",
      "https://example.com/guide",
    );
    expect(screen.getByRole("link", { name: "settings" })).toHaveAttribute("href", "/settings");
    expect(screen.queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
    expect(screen.getByText(/bad/)).toBeInTheDocument();
  });

  it("gives each code block a copy button with its own name", () => {
    render(<Markdown label="Backups" source={"```\none\n```\n\n```\ntwo\n```"} />);

    expect(screen.getByRole("button", { name: "Copy Backups step 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy Backups step 2" })).toBeInTheDocument();
  });
});
