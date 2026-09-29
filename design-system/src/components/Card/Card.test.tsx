import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Card } from "./Card";

describe("Card", () => {
  it("renders .card and adds .card-pad when pad", () => {
    const { container } = render(<Card pad>body</Card>);
    const el = container.querySelector("div")!;
    expect(el.className.split(" ")).toEqual(expect.arrayContaining(["card", "card-pad"]));
  });

  it("omits .card-pad by default and merges className", () => {
    const { container } = render(<Card className="tile" />);
    const el = container.querySelector("div")!;
    expect(el.className).toContain("card");
    expect(el.className).not.toContain("card-pad");
    expect(el.className).toContain("tile");
  });
});
