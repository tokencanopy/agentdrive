import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Badge } from "./Badge";

describe("Badge", () => {
  it("maps tone to .solid-{tone} and dot to .dot", () => {
    const { container } = render(
      <Badge tone="info" dot>
        unlisted
      </Badge>,
    );
    const el = container.querySelector("span")!;
    expect(el.className.split(" ")).toEqual(expect.arrayContaining(["badge", "solid-info", "dot"]));
  });

  it("is a bare .badge with no tone/dot by default", () => {
    const { container } = render(<Badge>v3</Badge>);
    const el = container.querySelector("span")!;
    expect(el.className.trim()).toBe("badge");
  });
});
