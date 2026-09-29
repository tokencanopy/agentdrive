import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { Modal } from "./Modal";

describe("Modal", () => {
  it("renders the scrim + dialog box and maps size", () => {
    const { container } = render(
      <Modal size="wide">
        <Modal.Head>head</Modal.Head>
        <Modal.Body>body</Modal.Body>
        <Modal.Foot>foot</Modal.Foot>
      </Modal>,
    );
    expect(container.querySelector(".scrim")).not.toBeNull();
    const box = container.querySelector(".modal")!;
    expect(box.className.split(" ")).toEqual(expect.arrayContaining(["modal", "modal-wide"]));
    expect(box.getAttribute("role")).toBe("dialog");
    expect(box.getAttribute("aria-modal")).toBe("true");
    expect(container.querySelector(".modal-head")).not.toBeNull();
    expect(container.querySelector(".modal-body")).not.toBeNull();
    expect(container.querySelector(".modal-foot")).not.toBeNull();
  });

  it("calls onClose on a backdrop click but not on a box click", () => {
    const onClose = vi.fn();
    const { container } = render(
      <Modal onClose={onClose}>
        <Modal.Body>body</Modal.Body>
      </Modal>,
    );
    fireEvent.click(container.querySelector(".modal")!);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(container.querySelector(".scrim")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
