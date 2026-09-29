import type { Meta, StoryObj } from "@storybook/react";
import { Radio } from "./forms";

const meta = {
  title: "Components/Radio",
  component: Radio,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Radio>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Group: Story = {
  render: () => (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <Radio name="vis" defaultChecked>
        Public
      </Radio>
      <Radio name="vis">Private</Radio>
      <Radio name="vis">Shared</Radio>
    </div>
  ),
};
