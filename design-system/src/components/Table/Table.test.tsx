import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Table } from "./Table";

describe("Table", () => {
  it("renders .tbl and adds .tbl-trunc when trunc", () => {
    const { container } = render(
      <Table trunc>
        <tbody>
          <tr>
            <td className="mono">reports/q1.md</td>
          </tr>
        </tbody>
      </Table>,
    );
    const t = container.querySelector("table")!;
    expect(t.className.split(" ")).toEqual(expect.arrayContaining(["tbl", "tbl-trunc"]));
    // native composition + .mono identifier cells are preserved
    expect(container.querySelector("td.mono")!.textContent).toBe("reports/q1.md");
  });
});
