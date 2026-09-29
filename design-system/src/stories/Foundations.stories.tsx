import type { Meta, StoryObj } from "@storybook/react";
import { KindChip, KINDS } from "../components/KindChip";

// Foundations — the token reference, replacing design/01-foundations.html.
// Reads live CSS custom properties from agentdrive.css (imported globally in
// .storybook/preview.tsx), so it always reflects the canonical source. Use the
// Theme toolbar to see cream vs. ink token sets.

const meta = {
  title: "Foundations/Tokens",
  parameters: { layout: "padded" },
} satisfies Meta;
export default meta;

type Story = StoryObj;

function Swatch({ name }: { name: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, fontFamily: "var(--f-mono)" }}>
      <span
        style={{
          width: 28,
          height: 28,
          borderRadius: "var(--r-sm)",
          background: `var(${name})`,
          border: "1px solid var(--border)",
          flex: "none",
        }}
      />
      <code>{name}</code>
    </div>
  );
}

function Group({ title, names }: { title: string; names: string[] }) {
  return (
    <section style={{ marginBottom: 20 }}>
      <h3 style={{ fontFamily: "var(--f-ui)", fontSize: 13, color: "var(--fg-muted)", margin: "0 0 8px" }}>{title}</h3>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 8 }}>
        {names.map((n) => (
          <Swatch key={n} name={n} />
        ))}
      </div>
    </section>
  );
}

export const Colors: Story = {
  render: () => (
    <div style={{ fontFamily: "var(--f-ui)" }}>
      <Group title="Drive shell (cream)" names={["--bg", "--bg-panel", "--bg-elev", "--bg-sunken"]} />
      <Group title="Ink (agent-native)" names={["--ink", "--ink-elev", "--ink-fg", "--ink-border"]} />
      <Group title="Accent (gold)" names={["--accent", "--accent-soft", "--accent-strong", "--accent-fill"]} />
      <Group title="Semantic" names={["--info", "--warn", "--danger", "--success"]} />
      <Group
        title="Kind hues"
        names={["--k-md", "--k-code", "--k-image", "--k-video", "--k-dataset", "--k-skill", "--k-bundle", "--k-folder"]}
      />
    </div>
  ),
};

export const Typography: Story = {
  render: () => (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ fontFamily: "var(--f-ui)" }}>
        <div style={{ fontSize: 12, color: "var(--fg-muted)", fontFamily: "var(--f-mono)" }}>--f-ui · Inter</div>
        <div style={{ fontSize: 24 }}>The quick brown fox — readable UI text</div>
      </div>
      <div style={{ fontFamily: "var(--f-mono)" }}>
        <div style={{ fontSize: 12, color: "var(--fg-muted)" }}>--f-mono · JetBrains Mono · all identifiers</div>
        <div style={{ fontSize: 18 }}>drv_4f2c… · art_91ab… · ad_live_…</div>
      </div>
    </div>
  ),
};

export const Radii: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 16, alignItems: "flex-end" }}>
      {["--r-sm", "--r-md", "--r-lg", "--r-xl"].map((r) => (
        <div key={r} style={{ textAlign: "center", fontFamily: "var(--f-mono)", fontSize: 11 }}>
          <div
            style={{
              width: 64,
              height: 64,
              background: "var(--bg-elev)",
              border: "1px solid var(--border)",
              borderRadius: `var(${r})`,
              marginBottom: 6,
            }}
          />
          {r}
        </div>
      ))}
    </div>
  ),
};

export const Kinds: Story = {
  render: () => (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {KINDS.map((k) => (
        <KindChip key={k} kind={k} />
      ))}
    </div>
  ),
};
