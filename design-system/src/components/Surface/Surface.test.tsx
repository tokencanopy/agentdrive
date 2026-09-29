import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Surface } from "./Surface";

describe("Surface", () => {
  it("sets data-theme so tokens re-resolve for the subtree", () => {
    const { container } = render(<Surface theme="dark">x</Surface>);
    const el = container.querySelector("div")!;
    expect(el.getAttribute("data-theme")).toBe("dark");
  });

  it("omits data-theme when no theme is given (inherits page theme)", () => {
    const { container } = render(<Surface>x</Surface>);
    expect(container.querySelector("div")!.hasAttribute("data-theme")).toBe(false);
  });

  it("paints the themed background/foreground and merges incoming style", () => {
    const { container } = render(<Surface theme="dark" style={{ padding: 8 }} />);
    const el = container.querySelector("div") as HTMLElement;
    expect(el.style.background).toBe("var(--bg)");
    expect(el.style.color).toBe("var(--fg)");
    expect(el.style.padding).toBe("8px");
  });
});
