import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Button } from "./Button";

describe("Button", () => {
  it("maps variant/size/mono to classes", () => {
    const { container } = render(
      <Button variant="danger" size="sm" mono>
        x
      </Button>,
    );
    const btn = container.querySelector("button")!;
    expect(btn.className.split(" ")).toEqual(expect.arrayContaining(["btn", "btn-danger", "btn-sm", "btn-mono"]));
  });

  it("defaults to primary variant and type=button", () => {
    const { container } = render(<Button>x</Button>);
    const btn = container.querySelector("button")!;
    expect(btn.className).toContain("btn-primary");
    expect(btn.getAttribute("type")).toBe("button");
  });

  it("merges incoming className instead of replacing owned classes", () => {
    const { container } = render(<Button className="extra">x</Button>);
    const btn = container.querySelector("button")!;
    expect(btn.className).toContain("btn");
    expect(btn.className).toContain("btn-primary");
    expect(btn.className).toContain("extra");
  });

  it("rejects an unknown variant at the type level", () => {
    // @ts-expect-error — "neon" is not a ButtonVariant
    render(<Button variant="neon">x</Button>);
  });
});
