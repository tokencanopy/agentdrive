import type { Meta, StoryObj } from "@storybook/react";
import { Badge } from "../Badge";
import { Button } from "../Button";
import { Card } from "../Card";
import { Input } from "../Input";
import { KindChip } from "../KindChip";
import { Table } from "../Table";
import { AgentTag } from "../agent/agent";
import { Toggle } from "../forms/forms";
import { Pill } from "../nav/nav";
import { Surface } from "./Surface";

// A representative slice of the catalog, used to show the full look in one card.
function Sampler() {
  return (
    <div style={{ display: "grid", gap: 16, maxWidth: 560 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <KindChip kind="md" />
        <strong className="mono">reports/q1-funnel.md</strong>
        <span className="vis public" style={{ marginLeft: "auto" }}>
          public
        </span>
      </div>

      <Card pad>
        <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
          <Pill active>All</Pill>
          <Pill>Markdown</Pill>
          <Pill>Code</Pill>
        </div>
        <Table>
          <thead>
            <tr>
              <th>path</th>
              <th>kind</th>
              <th>source</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="mono">leads.jsonl</td>
              <td>
                <KindChip kind="dataset" label="jsonl" />
              </td>
              <td>
                <AgentTag source="gpt">agent:gpt-research</AgentTag>
              </td>
            </tr>
            <tr>
              <td className="mono">parser.py</td>
              <td>
                <KindChip kind="code" label="python" />
              </td>
              <td>
                <AgentTag source="claude">agent:claude</AgentTag>
              </td>
            </tr>
          </tbody>
        </Table>
      </Card>

      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Input placeholder="Search artifacts…" />
        <Badge tone="accent">public</Badge>
        <Badge tone="success" dot>
          live
        </Badge>
        <Toggle defaultChecked />
        <Button variant="ghost">Cancel</Button>
        <Button variant="primary">Share</Button>
      </div>
    </div>
  );
}

const meta = {
  title: "Foundations/Surface",
  component: Surface,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Surface>;
export default meta;

type Story = StoryObj<typeof meta>;

/** The whole catalog re-themed dark via a single `<Surface theme="dark">`. */
export const Dark: Story = {
  render: () => (
    <Surface theme="dark" style={{ padding: 24, borderRadius: 12 }}>
      <Sampler />
    </Surface>
  ),
};

/** The same components on the default cream surface, for side-by-side comparison. */
export const Light: Story = {
  render: () => (
    <Surface theme="light" style={{ padding: 24, borderRadius: 12 }}>
      <Sampler />
    </Surface>
  ),
};
