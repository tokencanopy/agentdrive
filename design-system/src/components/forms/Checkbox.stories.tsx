import type { Meta, StoryObj } from "@storybook/react";
import { Checkbox } from "./forms";

const meta = {
  title: "Components/Checkbox",
  component: Checkbox,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Checkbox>;
export default meta;

type Story = StoryObj<typeof meta>;

export const States: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
      <Checkbox defaultChecked>Checked</Checkbox>
      <Checkbox>Unchecked</Checkbox>
      <Checkbox disabled>Disabled</Checkbox>
    </div>
  ),
};
