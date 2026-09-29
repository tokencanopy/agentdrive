import type { Meta, StoryObj } from "@storybook/react";
import { Pill } from "./nav";

const meta = {
  title: "Components/Pill",
  component: Pill,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Pill>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Filters: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 6 }}>
      <Pill active>All</Pill>
      <Pill>Markdown</Pill>
      <Pill>Code</Pill>
      <Pill>Datasets</Pill>
    </div>
  ),
};
