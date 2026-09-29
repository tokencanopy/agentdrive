import type { Meta, StoryObj } from "@storybook/react";
import { Alert } from "./feedback";

const meta = {
  title: "Components/Alert",
  component: Alert,
  args: { children: "Heads up — this is an inline alert." },
  parameters: { layout: "padded" },
} satisfies Meta<typeof Alert>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Tones: Story = {
  render: () => (
    <div style={{ display: "grid", gap: 8, maxWidth: 480 }}>
      <Alert tone="info">Info — your drive is syncing.</Alert>
      <Alert tone="warn">Warning — storage is 90% full.</Alert>
      <Alert tone="danger">Error — upload failed (trace ID 4f2c).</Alert>
      <Alert tone="success">Saved — published to a public URL.</Alert>
    </div>
  ),
};
