import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { KindChip } from "./KindChip";
import type { KindChipProps } from "./KindChip";

describe("KindChip", () => {
  it("sets data-k, the .kind class, a glyph, and a default label", () => {
    const { container } = render(<KindChip kind="skill" />);
    const el = container.querySelector("span")!;
    expect(el.className).toContain("kind");
    expect(el.getAttribute("data-k")).toBe("skill");
    expect(el.querySelector("svg")).not.toBeNull(); // color is paired with a glyph (CVD-robust)
    expect(el.textContent).toContain("skill");
  });

  it("allows a label override distinct from the kind", () => {
    const { container } = render(<KindChip kind="code" label="python" />);
    const el = container.querySelector("span")!;
    expect(el.getAttribute("data-k")).toBe("code");
    expect(el.textContent).toContain("python");
  });

  it("keeps data-k component-owned (a forced data-k cannot override it)", () => {
    // Force an out-of-contract data-k through a double cast; the component sets
    // data-k after the spread, so it must win.
    const forced = { kind: "md", "data-k": "code" } as unknown as KindChipProps;
    const { container } = render(createElement(KindChip, forced));
    expect(container.querySelector("span")!.getAttribute("data-k")).toBe("md");
  });

  it("rejects an unknown kind at the type level", () => {
    // @ts-expect-error — "spreadsheet" is not a Kind
    render(<KindChip kind="spreadsheet" />);
  });
});
