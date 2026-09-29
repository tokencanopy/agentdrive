import type { Meta, StoryObj } from "@storybook/react";
import { KindChip } from "../KindChip";
import { Table } from "./Table";

const meta = {
  title: "Components/Table",
  component: Table,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Table>;
export default meta;

type Story = StoryObj<typeof meta>;

export const ArtifactList: Story = {
  render: () => (
    <Table>
      <thead>
        <tr>
          <th>path</th>
          <th>kind</th>
          <th>size</th>
          <th>source</th>
          <th>updated</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td className="mono">reports/q1-funnel.md</td>
          <td>
            <KindChip kind="md" />
          </td>
          <td>12.4 KB</td>
          <td>
            <span className="agent-tag">agent:claude-sonnet-4-6</span>
          </td>
          <td className="mono">12s</td>
        </tr>
        <tr>
          <td className="mono">leads.jsonl</td>
          <td>
            <KindChip kind="dataset" label="jsonl" />
          </td>
          <td>4.1 MB</td>
          <td>
            <span className="agent-tag gpt">agent:gpt-research</span>
          </td>
          <td className="mono">3m</td>
        </tr>
        <tr>
          <td className="mono">parser.py</td>
          <td>
            <KindChip kind="code" label="python" />
          </td>
          <td>3.6 KB</td>
          <td>
            <span className="agent-tag user">user upload</span>
          </td>
          <td className="mono">5h</td>
        </tr>
      </tbody>
    </Table>
  ),
};
