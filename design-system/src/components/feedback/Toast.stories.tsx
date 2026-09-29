import type { Meta, StoryObj } from "@storybook/react";
import { Toast } from "./feedback";

const meta = {
  title: "Components/Toast",
  component: Toast,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Toast>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Variants: Story = {
  render: () => (
    <div style={{ display: "grid", gap: 8, maxWidth: 360 }}>
      <Toast tone="success">
        <Toast.Icon>✓</Toast.Icon>
        <div>
          <Toast.Title>Published</Toast.Title>
          <Toast.Sub>reports/q1-funnel.md is now public.</Toast.Sub>
        </div>
      </Toast>
      <Toast tone="danger">
        <Toast.Icon>!</Toast.Icon>
        <div>
          <Toast.Title>Upload failed</Toast.Title>
          <Toast.Sub>Network error — retry.</Toast.Sub>
        </div>
      </Toast>
    </div>
  ),
};
