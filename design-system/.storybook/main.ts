import type { StorybookConfig } from "@storybook/react-vite";

// Storybook is the dev workshop (replacing the retired design/ HTML mockups)
// AND the high-fidelity preview source for the design-sync skill (shape:
// "storybook"). design-sync runs from design-system/, so this config dir is
// referenced as ".storybook" relative to that cwd.
const config: StorybookConfig = {
  stories: ["../src/**/*.stories.@(ts|tsx|mdx)"],
  addons: ["@storybook/addon-essentials", "@storybook/addon-a11y"],
  framework: {
    name: "@storybook/react-vite",
    options: {},
  },
  core: {
    disableTelemetry: true,
  },
};

export default config;
