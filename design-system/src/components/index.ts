// Component barrel — the full catalog.
// Slice 1 (core): Button, Card, Badge, Input, KindChip, Modal, Table.
export * from "./Button";
export * from "./Card";
export * from "./Badge";
export * from "./Input";
export * from "./KindChip";
export * from "./Modal";
export * from "./Table";

// Slice 4 (grouped primitives):
export * from "./forms/forms"; // Checkbox, Radio, Toggle
export * from "./feedback/feedback"; // Alert, Toast, Progress, Spinner, Pulse, Skeleton, Empty
export * from "./agent/agent"; // AgentTag, VisibilityPill, Console
export * from "./nav/nav"; // Pill, Tabs, Avatar

// Theming:
export * from "./Surface"; // Surface — scope data-theme (cream/dark) to a region
