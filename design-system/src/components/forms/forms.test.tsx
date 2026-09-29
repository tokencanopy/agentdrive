import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Checkbox, Radio, Toggle } from "./forms";

describe("form controls", () => {
  it("Checkbox is a .check label wrapping a checkbox input + label text", () => {
    const { container } = render(<Checkbox className="x">Filter</Checkbox>);
    const label = container.querySelector("label.check")!;
    expect(label.className.split(" ")).toEqual(expect.arrayContaining(["check", "x"]));
    expect(label.querySelector("input")!.getAttribute("type")).toBe("checkbox");
    expect(label.textContent).toContain("Filter");
  });

  it("Radio is a .radio label wrapping a radio input", () => {
    const { container } = render(<Radio name="vis">Public</Radio>);
    const label = container.querySelector("label.radio")!;
    expect(label.querySelector("input")!.getAttribute("type")).toBe("radio");
    expect(label.querySelector("input")!.getAttribute("name")).toBe("vis");
  });

  it("Toggle is a .toggle label wrapping a switch input + .slider", () => {
    const { container } = render(<Toggle defaultChecked />);
    const label = container.querySelector("label.toggle")!;
    const input = label.querySelector("input")!;
    expect(input.getAttribute("type")).toBe("checkbox");
    expect(input.getAttribute("role")).toBe("switch");
    expect(label.querySelector("span.slider")).not.toBeNull();
  });

  it("forwards props/ref to the inner input (e.g. disabled, checked)", () => {
    const { container } = render(<Checkbox disabled defaultChecked />);
    const input = container.querySelector("input")!;
    expect(input.hasAttribute("disabled")).toBe(true);
    expect((input as HTMLInputElement).checked).toBe(true);
  });
});
