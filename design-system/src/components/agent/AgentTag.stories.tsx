import type { Meta, StoryObj } from "@storybook/react";
import { AgentTag } from "./agent";

const meta = {
  title: "Components/AgentTag",
  component: AgentTag,
  parameters: { layout: "padded" },
} satisfies Meta<typeof AgentTag>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Sources: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      <AgentTag source="claude">agent:claude-sonnet-4-6</AgentTag>
      <AgentTag source="gpt">agent:gpt-research</AgentTag>
      <AgentTag source="user">user upload</AgentTag>
      <AgentTag>agent:unnamed</AgentTag>
    </div>
  ),
};
