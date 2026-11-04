import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DevelopmentModeBanner } from "./DevelopmentModeBanner";

describe("DevelopmentModeBanner", () => {
  it("says review can be off and the evidence is not for an audit", () => {
    render(<DevelopmentModeBanner />);
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("Development mode.");
    expect(banner).toHaveTextContent("Evidence from this instance is not audit evidence.");
  });
});
