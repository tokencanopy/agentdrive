import type { Meta, StoryObj } from "@storybook/react";
import { VisibilityPill } from "./agent";

const meta = {
  title: "Components/VisibilityPill",
  component: VisibilityPill,
  args: { state: "public" },
  parameters: { layout: "padded" },
} satisfies Meta<typeof VisibilityPill>;
export default meta;

type Story = StoryObj<typeof meta>;

export const States: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 8 }}>
      <VisibilityPill state="public" />
      <VisibilityPill state="private" />
      <VisibilityPill state="shared" />
    </div>
  ),
};
