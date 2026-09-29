import type { Meta, StoryObj } from "@storybook/react";
import { KindChip, KINDS } from "./KindChip";

const meta = {
  title: "Components/KindChip",
  component: KindChip,
  args: { kind: "md" },
  parameters: { layout: "padded" },
} satisfies Meta<typeof KindChip>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Single: Story = { args: { kind: "skill" } };

export const AllKinds: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {KINDS.map((k) => (
        <KindChip key={k} kind={k} />
      ))}
    </div>
  ),
};

export const LabelOverride: Story = { args: { kind: "code", label: "python" } };
