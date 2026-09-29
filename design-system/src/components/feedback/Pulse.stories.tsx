import type { Meta, StoryObj } from "@storybook/react";
import { Pulse } from "./feedback";

const meta = {
  title: "Components/Pulse",
  component: Pulse,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Pulse>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Live: Story = {
  render: () => (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center", fontFamily: "var(--f-mono)", fontSize: 12 }}>
      <Pulse /> agent writing…
    </span>
  ),
};
