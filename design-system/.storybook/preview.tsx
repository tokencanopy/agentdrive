import type { Decorator, Preview } from "@storybook/react";

// The canonical stylesheet — imported globally so every story renders against
// the real agentdrive.css tokens + primitives. This single import is what makes
// the previews faithful (and what design-sync captures as styles.css).
import "../src/styles/agentdrive.css";

// Drive the two-surface token set exactly the way the app does: a `data-theme`
// on the document root (`light` | `dark`). The app resolves this pre-paint from
// the `agentdrive-theme` localStorage key; in Storybook it's a toolbar global.
const withTheme: Decorator = (Story, context) => {
  const theme = (context.globals.theme as "light" | "dark") ?? "light";
  if (typeof document !== "undefined") {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }
  return (
    <div data-theme={theme} style={{ background: "var(--bg)", color: "var(--fg)", minHeight: "100vh", padding: 24 }}>
      <Story />
    </div>
  );
};

const preview: Preview = {
  globalTypes: {
    theme: {
      description: "AgentDrive theme (cream drive shell / ink agent surface)",
      defaultValue: "light",
      toolbar: {
        title: "Theme",
        icon: "circlehollow",
        items: [
          { value: "light", title: "Light (cream)" },
          { value: "dark", title: "Dark" },
        ],
        dynamicTitle: true,
      },
    },
  },
  decorators: [withTheme],
  parameters: {
    layout: "fullscreen",
    controls: { expanded: true },
  },
};

export default preview;
