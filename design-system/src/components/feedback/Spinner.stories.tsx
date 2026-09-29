import type { Meta, StoryObj } from "@storybook/react";
import { Spinner } from "./feedback";

const meta = {
  title: "Components/Spinner",
  component: Spinner,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Spinner>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Inline: Story = {
  render: () => (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
      <Spinner /> Loading…
    </span>
  ),
};
