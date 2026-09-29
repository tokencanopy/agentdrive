import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Avatar, Pill, Tabs } from "./nav";

describe("nav / misc primitives", () => {
  it("Pill maps active to .pill.active and defaults type=button", () => {
    const { container } = render(<Pill active>All</Pill>);
    const el = container.querySelector("button")!;
    expect(el.className.split(" ")).toEqual(expect.arrayContaining(["pill", "active"]));
    expect(el.getAttribute("type")).toBe("button");
  });

  it("Tabs renders a tablist; Tab.active sets .active + aria-selected", () => {
    const { container } = render(
      <Tabs>
        <Tabs.Tab active>A</Tabs.Tab>
        <Tabs.Tab>B</Tabs.Tab>
      </Tabs>,
    );
    expect(container.querySelector(".tabs")!.getAttribute("role")).toBe("tablist");
    const active = container.querySelector("button.active")!;
    expect(active.getAttribute("aria-selected")).toBe("true");
    expect(active.textContent).toBe("A");
  });

  it("Avatar sets data-h, composes size + square", () => {
    const { container } = render(
      <Avatar hash={3} size="lg" square>
        JZ
      </Avatar>,
    );
    const el = container.querySelector("span")!;
    expect(el.getAttribute("data-h")).toBe("3");
    expect(el.className.split(" ")).toEqual(expect.arrayContaining(["av", "sq", "lg"]));
  });

  it("Avatar wraps hash into the 0–7 palette range", () => {
    expect(render(<Avatar hash={10}>x</Avatar>).container.querySelector("span")!.getAttribute("data-h")).toBe("2");
    expect(render(<Avatar hash={-1}>x</Avatar>).container.querySelector("span")!.getAttribute("data-h")).toBe("7");
  });
});
