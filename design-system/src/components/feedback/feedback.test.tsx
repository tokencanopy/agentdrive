import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Alert, Empty, Progress, Pulse, Skeleton, Spinner, Toast } from "./feedback";

describe("feedback primitives", () => {
  it("Alert maps tone to .alert-{tone} and is role=alert", () => {
    const { container } = render(<Alert tone="danger">x</Alert>);
    const el = container.querySelector("div")!;
    expect(el.className.split(" ")).toEqual(expect.arrayContaining(["alert", "alert-danger"]));
    expect(el.getAttribute("role")).toBe("alert");
  });

  it("Toast maps tone + renders compound parts", () => {
    const { container } = render(
      <Toast tone="success">
        <Toast.Icon>✓</Toast.Icon>
        <Toast.Title>t</Toast.Title>
        <Toast.Sub>s</Toast.Sub>
      </Toast>,
    );
    expect(container.querySelector(".toast.success")).not.toBeNull();
    expect(container.querySelector(".toast-ico")).not.toBeNull();
    expect(container.querySelector(".toast-ttl")).not.toBeNull();
    expect(container.querySelector(".toast-sub")).not.toBeNull();
  });

  it("Progress sets the bar width from value and role=progressbar", () => {
    const { container } = render(<Progress value={62} />);
    const root = container.querySelector(".progress")!;
    expect(root.getAttribute("role")).toBe("progressbar");
    expect(root.getAttribute("aria-valuenow")).toBe("62");
    expect((container.querySelector(".bar") as HTMLElement).style.width).toBe("62%");
  });

  it("Progress indeterminate adds the modifier and drops aria-valuenow", () => {
    const { container } = render(<Progress indeterminate />);
    expect(container.querySelector(".progress.indeterminate")).not.toBeNull();
    expect(container.querySelector(".progress")!.getAttribute("aria-valuenow")).toBeNull();
  });

  it("Skeleton composes variant + line width", () => {
    const { container } = render(<Skeleton variant="line" width="short" />);
    expect(container.querySelector("div")!.className.split(" ")).toEqual(
      expect.arrayContaining(["skeleton", "line", "short"]),
    );
  });

  it("Spinner/Pulse/Empty are bare class wrappers", () => {
    expect(render(<Spinner />).container.querySelector("span")!.className.trim()).toBe("spinner");
    expect(render(<Pulse />).container.querySelector("span")!.className.trim()).toBe("pulse");
    expect(render(<Empty />).container.querySelector("div")!.className.trim()).toBe("empty");
  });
});
