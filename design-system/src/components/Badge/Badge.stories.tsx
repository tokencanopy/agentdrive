import type { Meta, StoryObj } from "@storybook/react";
import { Badge } from "./Badge";

const meta = {
  title: "Components/Badge",
  component: Badge,
  args: { children: "badge" },
  parameters: { layout: "padded" },
} satisfies Meta<typeof Badge>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Default: Story = { args: { children: "v3" } };

export const Tones: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
      <Badge>12.4 KB</Badge>
      <Badge tone="accent">public</Badge>
      <Badge tone="info">unlisted</Badge>
      <Badge tone="warn">draft</Badge>
      <Badge tone="danger">error</Badge>
      <Badge tone="success">ready</Badge>
    </div>
  ),
};

export const LiveDot: Story = {
  render: () => (
    <Badge tone="success" dot>
      live
    </Badge>
  ),
};
