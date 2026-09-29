import type { Meta, StoryObj } from "@storybook/react";
import { Empty } from "./feedback";

const meta = {
  title: "Components/Empty",
  component: Empty,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Empty>;
export default meta;

type Story = StoryObj<typeof meta>;

export const ZeroState: Story = {
  args: {
    style: { maxWidth: 360 },
    children: "No artifacts yet — your agents' output will appear here.",
  },
};
