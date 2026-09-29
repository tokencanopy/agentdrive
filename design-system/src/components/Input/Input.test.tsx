import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Input } from "./Input";

describe("Input", () => {
  it("adds .mono and forwards native attributes", () => {
    const { container } = render(<Input mono placeholder="drv_…/path" />);
    const el = container.querySelector("input")!;
    expect(el.className.split(" ")).toEqual(expect.arrayContaining(["input", "mono"]));
    expect(el.getAttribute("placeholder")).toBe("drv_…/path");
  });

  it("is a bare .input by default", () => {
    const { container } = render(<Input />);
    const el = container.querySelector("input")!;
    expect(el.className.trim()).toBe("input");
  });
});
