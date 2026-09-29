import type { Meta, StoryObj } from "@storybook/react";
import { Card } from "./Card";

const meta = {
  title: "Components/Card",
  component: Card,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Card>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Padded: Story = {
  args: {
    pad: true,
    style: { maxWidth: 320 },
    children: (
      <>
        <strong style={{ display: "block", marginBottom: 6 }}>reports/q1-funnel.md</strong>
        <span style={{ color: "var(--fg-muted)", fontSize: 13 }}>A standard elevated panel with internal padding.</span>
      </>
    ),
  },
};

export const Unpadded: Story = {
  args: {
    style: { maxWidth: 320 },
    children: <div style={{ padding: 16 }}>No card-pad — caller controls padding.</div>,
  },
};
