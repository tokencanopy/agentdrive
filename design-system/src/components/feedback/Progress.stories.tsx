import type { Meta, StoryObj } from "@storybook/react";
import { Progress } from "./feedback";

const meta = {
  title: "Components/Progress",
  component: Progress,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Progress>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Determinate: Story = { args: { value: 62 } };
export const Indeterminate: Story = { args: { indeterminate: true } };
