import type { Meta, StoryObj } from "@storybook/react";
import { Skeleton } from "./feedback";

const meta = {
  title: "Components/Skeleton",
  component: Skeleton,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Skeleton>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Variants: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 16, alignItems: "flex-start", maxWidth: 360 }}>
      <Skeleton variant="thumb" />
      <div style={{ flex: 1 }}>
        <Skeleton variant="title" />
        <Skeleton variant="line" width="long" />
        <Skeleton variant="line" width="med" />
        <Skeleton variant="line" width="short" />
      </div>
    </div>
  ),
};
