import type { Meta, StoryObj } from "@storybook/react";
import { Toggle } from "./forms";

const meta = {
  title: "Components/Toggle",
  component: Toggle,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Toggle>;
export default meta;

type Story = StoryObj<typeof meta>;

export const States: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
      <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
        <Toggle defaultChecked /> On
      </span>
      <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
        <Toggle /> Off
      </span>
    </div>
  ),
};
