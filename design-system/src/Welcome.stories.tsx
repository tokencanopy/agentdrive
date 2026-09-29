import type { Meta, StoryObj } from "@storybook/react";

// Slice 0 placeholder story — gives Storybook something to build while the real
// component stories land in Slice 1. It also smoke-tests that the global
// agentdrive.css import and the theme decorator render (toggle the toolbar
// Theme control to see cream vs. ink tokens flip).
function Welcome() {
  return (
    <div style={{ fontFamily: "var(--f-ui)", maxWidth: 640 }}>
      <h1 style={{ marginTop: 0 }}>AgentDrive Design System</h1>
      <p style={{ color: "var(--fg-muted)" }}>
        React components over the canonical <code>agentdrive.css</code>. This package owns the
        stylesheet; the running app consumes a generated copy.
      </p>
      <p>
        Core components (Button, Card, Kind chip, Badge, Input, Modal, Table) live under{" "}
        <strong>Components</strong>; the token reference is under <strong>Foundations</strong>. Use the{" "}
        <strong>Theme</strong> toolbar control to switch between the cream drive shell and the ink surface.
      </p>
    </div>
  );
}

const meta: Meta<typeof Welcome> = {
  title: "Foundations/Welcome",
  component: Welcome,
};
export default meta;

type Story = StoryObj<typeof Welcome>;

export const Overview: Story = {};
