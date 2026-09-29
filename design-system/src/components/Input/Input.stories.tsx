import type { Meta, StoryObj } from "@storybook/react";
import { Input } from "./Input";

const meta = {
  title: "Components/Input",
  component: Input,
  args: { placeholder: "email address" },
  parameters: { layout: "padded" },
} satisfies Meta<typeof Input>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Default: Story = {};

export const Mono: Story = { args: { mono: true, placeholder: "drv_…/reports/q1.md" } };

export const Filled: Story = { args: { defaultValue: "hello@example.com" } };

export const Disabled: Story = { args: { disabled: true, defaultValue: "read-only" } };
