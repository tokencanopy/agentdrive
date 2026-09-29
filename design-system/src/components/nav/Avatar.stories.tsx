import type { Meta, StoryObj } from "@storybook/react";
import { Avatar } from "./nav";

const meta = {
  title: "Components/Avatar",
  component: Avatar,
  args: { hash: 0, children: "JZ" },
  parameters: { layout: "padded" },
} satisfies Meta<typeof Avatar>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Palette: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      {Array.from({ length: 8 }, (_, h) => (
        <Avatar key={h} hash={h}>
          {String.fromCharCode(65 + h)}
        </Avatar>
      ))}
    </div>
  ),
};

export const Sizes: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <Avatar hash={2} size="sm">A</Avatar>
      <Avatar hash={2}>A</Avatar>
      <Avatar hash={2} size="lg">A</Avatar>
      <Avatar hash={2} size="xl">A</Avatar>
      <Avatar hash={3} square>S</Avatar>
    </div>
  ),
};
