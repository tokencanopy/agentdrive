import type { Meta, StoryObj } from "@storybook/react";
import { Console } from "./agent";

const meta = {
  title: "Components/Console",
  component: Console,
  parameters: { layout: "padded" },
} satisfies Meta<typeof Console>;
export default meta;

type Story = StoryObj<typeof meta>;

export const McpSnippet: Story = {
  render: () => (
    <Console>
      <Console.Comment># publish an artifact to a public URL</Console.Comment>
      {"\n"}
      <Console.Prompt>$</Console.Prompt> agentdrive publish <Console.String>reports/q1-funnel.md</Console.String>
      {"\n"}
      <Console.Key>url</Console.Key>: <Console.String>https://share.tokencanopy.com/a/art_4f2c</Console.String>
    </Console>
  ),
};
