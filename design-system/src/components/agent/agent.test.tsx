import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { AgentTag, Console, VisibilityPill } from "./agent";

describe("agent-context primitives", () => {
  it("AgentTag maps source to .agent-tag.{source}", () => {
    const { container } = render(<AgentTag source="gpt">x</AgentTag>);
    expect(container.querySelector(".agent-tag.gpt")).not.toBeNull();
  });

  it("AgentTag is a bare .agent-tag with no source", () => {
    const { container } = render(<AgentTag>x</AgentTag>);
    expect(container.querySelector("span")!.className.trim()).toBe("agent-tag");
  });

  it("VisibilityPill maps state to .vis.{state} and defaults its label to the state", () => {
    const { container } = render(<VisibilityPill state="shared" />);
    const el = container.querySelector("span")!;
    expect(el.className.split(" ")).toEqual(expect.arrayContaining(["vis", "shared"]));
    expect(el.textContent).toBe("shared");
  });

  it("Console renders a .console <pre> and colored part spans", () => {
    const { container } = render(
      <Console>
        <Console.Prompt>$</Console.Prompt>
        <Console.Comment>#</Console.Comment>
        <Console.String>s</Console.String>
        <Console.Key>k</Console.Key>
      </Console>,
    );
    expect(container.querySelector("pre.console")).not.toBeNull();
    expect(container.querySelector(".c-prompt")).not.toBeNull();
    expect(container.querySelector(".c-comment")).not.toBeNull();
    expect(container.querySelector(".c-string")).not.toBeNull();
    expect(container.querySelector(".c-key")).not.toBeNull();
  });
});
