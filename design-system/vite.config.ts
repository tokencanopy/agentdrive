import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import dts from "vite-plugin-dts";
import { defineConfig } from "vite";

// Library mode. Emits a typed ESM + UMD bundle of the AgentDrive component
// library. React/ReactDOM are externalized — the design-sync converter
// re-bundles dist/ and vendors React for the Claude Design runtime, so this
// build exists for local dev, Storybook, and a clean typed dist/, NOT to
// produce the artifact Claude Design ultimately consumes.
export default defineConfig({
  plugins: [
    react(),
    dts({ include: ["src"], exclude: ["src/**/*.stories.tsx", "src/**/*.test.tsx"], rollupTypes: false }),
  ],
  build: {
    lib: {
      entry: resolve(__dirname, "src/index.ts"),
      name: "AgentDriveDS",
      fileName: "agentdrive-ds",
      formats: ["es", "umd"],
    },
    rollupOptions: {
      external: ["react", "react-dom", "react/jsx-runtime"],
      output: {
        globals: {
          react: "React",
          "react-dom": "ReactDOM",
          "react/jsx-runtime": "jsxRuntime",
        },
      },
    },
  },
});
