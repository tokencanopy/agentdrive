import type { Meta, StoryObj } from "@storybook/react";
import { Button } from "../Button";
import { KindChip } from "../KindChip";
import { Modal } from "./Modal";

const meta = {
  title: "Components/Modal",
  component: Modal,
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof Modal>;
export default meta;

type Story = StoryObj<typeof meta>;

export const ShareSheet: Story = {
  render: () => (
    <Modal size="wide">
      <Modal.Head>
        <KindChip kind="md" />
        <h3 style={{ margin: 0, fontSize: 15 }}>
          Share <span className="mono" style={{ color: "var(--fg-muted)" }}>reports/q1-funnel.md</span>
        </h3>
      </Modal.Head>
      <Modal.Body>
        <p style={{ marginTop: 0, color: "var(--fg-muted)" }}>
          Anyone with the link can view this artifact and its public URL.
        </p>
        <input className="input mono" defaultValue="https://share.tokencanopy.com/a/art_4f2c…" style={{ width: "100%" }} />
      </Modal.Body>
      <Modal.Foot>
        <Button variant="ghost">Cancel</Button>
        <Button variant="primary">Copy link</Button>
      </Modal.Foot>
    </Modal>
  ),
};

export const Small: Story = {
  render: () => (
    <Modal size="sm">
      <Modal.Head>
        <h3 style={{ margin: 0, fontSize: 15 }}>Delete artifact?</h3>
      </Modal.Head>
      <Modal.Body>This cannot be undone.</Modal.Body>
      <Modal.Foot>
        <Button variant="ghost">Cancel</Button>
        <Button variant="danger">Delete</Button>
      </Modal.Foot>
    </Modal>
  ),
};
