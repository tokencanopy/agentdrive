import type { Meta, StoryObj } from "@storybook/react";
import { Tabs } from "./nav";

const meta = {
  title: "Components/Tabs",
  component: Tabs,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Tabs>;
export default meta;

type Story = StoryObj<typeof meta>;

export const Default: Story = {
  render: () => (
    <Tabs>
      <Tabs.Tab active>Preview</Tabs.Tab>
      <Tabs.Tab>Metadata</Tabs.Tab>
      <Tabs.Tab>Versions</Tabs.Tab>
      <Tabs.Tab>Access</Tabs.Tab>
    </Tabs>
  ),
};
