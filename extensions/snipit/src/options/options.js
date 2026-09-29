// SnipIt settings: where captures go, and what happens after one.
//
// DOM wiring only. Every rule it enforces lives in a tested module:
// `lib/settings.js` (what is storable), `lib/first-run.js` (when a
// proposal is safe), `lib/config.js` (which endpoints are legal). This
// file's own job is to ask Hub and AgentDrive the right questions in the
// right order and render the answers.

import {
  clearEndpointOverride,
  ENVIRONMENTS,
  getEndpoints,
  readEndpointOverride,
  setEndpointOverride,
} from "../lib/config.js";
import {
  createFolder,
  listChildFolders,
  listDrives,
  readDrive,
} from "../lib/drive-api.js";
import { clearDriveTokens, driveToken } from "../lib/drive-token.js";
import { listWorkspaces } from "../lib/hub-api.js";
import { isSignedIn, signIn, signOut } from "../lib/hub-auth.js";
import { proposeLocation } from "../lib/first-run.js";
import {
  clearLocation,
  DEFAULT_PREFERENCES,
  readSettings,
  updatePreferences,
  writeLocation,
} from "../lib/settings.js";

const main = document.getElementById("main");
const envLabel = document.getElementById("env");

/** Everything the page knows. Re-rendered wholesale; the page is small
 *  enough that partial updates would only add ways to be inconsistent. */
let view = {
  loading: true,
  signedIn: false,
  settings: null,
  endpoints: null,
  error: null,
  /** The in-progress location picker, or null when it is closed. */
  picker: null,
};

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(child);
  }
  return node;
}

function section(title, children) {
  return el("section", {}, [el("h2", { text: title }), ...[].concat(children)]);
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function crumbs(location) {
  return [location.workspace_name, location.drive_name, ...(location.folder_path ?? [])]
    .filter(Boolean)
    .join(" › ");
}

async function reload() {
  view.settings = await readSettings();
  view.signedIn = await isSignedIn();
  view.endpoints = await getEndpoints();
  view.loading = false;
  render();
}

/** Run an async action, showing its failure rather than swallowing it. */
async function guard(action) {
  view.error = null;
  render();
  try {
    await action();
  } catch (error) {
    view.error = String(error?.message ?? error);
  }
  await reload();
}

// ---------------------------------------------------------------------------
// The location picker: workspace → drive → folder
// ---------------------------------------------------------------------------

async function openPicker() {
  const { items } = await listWorkspaces({ limit: 100 });
  view.picker = { step: "workspace", workspaces: items };
}

/**
 * A usable drive token for the workspace being browsed.
 *
 * Called per request, NOT held on `view.picker`. A drive token lives five
 * minutes; anyone who opens the picker, browses a folder tree, thinks for a
 * while and then clicks "Save captures here" would otherwise get a bare
 * "token was rejected" with no way forward but starting over. `driveToken`
 * is session-cached and re-mints at 30s to expiry, so in the common case
 * this costs nothing.
 */
async function pickerToken(access = "browse") {
  return await driveToken(view.picker.workspace.id, access);
}

async function chooseWorkspace(workspace) {
  const token = await driveToken(workspace.id, "browse");
  const drives = await listDrives(token);
  view.picker = { step: "drive", workspace, drives };
}

async function chooseDrive(drive) {
  const { workspace } = view.picker;
  const token = await pickerToken();
  // `listDrives` already returns the root; read it again only if a
  // listing ever stops carrying it.
  const rootId =
    drive.root_folder_id ?? (await readDrive(token, drive.id)).root_folder_id;
  view.picker = {
    step: "folder",
    workspace,
    drive,
    // The breadcrumb trail from the drive root to where we are browsing.
    trail: [{ id: rootId, name: drive.name }],
    children: (await listChildFolders(token, drive.id, rootId)).items,
  };
}

async function browseInto(folder) {
  const { drive, trail } = view.picker;
  const token = await pickerToken();
  view.picker = {
    ...view.picker,
    trail: [...trail, { id: folder.id, name: folder.name }],
    children: (await listChildFolders(token, drive.id, folder.id)).items,
  };
}

async function browseTo(index) {
  const { drive, trail } = view.picker;
  const token = await pickerToken();
  const next = trail.slice(0, index + 1);
  view.picker = {
    ...view.picker,
    trail: next,
    children: (await listChildFolders(token, drive.id, next[next.length - 1].id))
      .items,
  };
}

async function createFolderHere(name) {
  const { drive, trail } = view.picker;
  // The one WRITE the picker performs, so it is the one place that needs
  // more than the read-only browse token.
  const token = await pickerToken("capture");
  const parent = trail[trail.length - 1];
  const created = await createFolder(token, drive.id, {
    parentId: parent.id,
    name,
  });
  await browseInto({ id: created.id, name: created.name ?? name });
}

async function saveCurrentFolder() {
  const { workspace, drive, trail } = view.picker;
  const here = trail[trail.length - 1];
  await writeLocation({
    workspace_id: workspace.id,
    workspace_name: workspace.display_name,
    drive_id: drive.id,
    drive_name: drive.name,
    folder_id: here.id,
    // The trail's first entry is the drive itself, not a folder.
    folder_path: trail.slice(1).map((entry) => entry.name),
  });
  view.picker = null;
}

/** First run: propose only when there is nothing to choose between. */
async function proposeFirstRun() {
  // Two, not one: the proposal is only safe when there is EXACTLY one, and
  // asking for one back can never tell us whether a second exists.
  const { items: workspaces } = await listWorkspaces({ limit: 2 });
  if (workspaces.length !== 1) return null;

  const token = await driveToken(workspaces[0].id, "browse");
  const drives = await listDrives(token, { limit: 2 });
  const proposal = proposeLocation({ workspaces, drives });
  if (!proposal) return null;
  return { proposal, token };
}

async function acceptProposal({ proposal, token }) {
  // Create the folder if it is not already there — a second run of the
  // proposal must not make "Screenshots (1)".
  let folder = null;
  const existing = await listChildFolders(
    token,
    proposal.drive_id,
    proposal.parent_folder_id,
  );
  folder =
    existing.items.find((item) => item.name === proposal.folder_name) ?? null;
  if (!folder) {
    // The read token cannot create; ask for the write one only at the
    // point something is actually written.
    const writeToken = await driveToken(proposal.workspace_id, "capture");
    folder = await createFolder(writeToken, proposal.drive_id, {
      parentId: proposal.parent_folder_id,
      name: proposal.folder_name,
    });
  }
  await writeLocation({
    workspace_id: proposal.workspace_id,
    workspace_name: proposal.workspace_name,
    drive_id: proposal.drive_id,
    drive_name: proposal.drive_name,
    folder_id: folder.id,
    folder_path: [proposal.folder_name],
  });
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderPicker() {
  const picker = view.picker;
  const cancel = el("button", {
    className: "secondary",
    text: "Cancel",
    onClick: () => {
      view.picker = null;
      render();
    },
  });

  if (picker.step === "workspace") {
    const list = el("ul", { className: "picker" },
      picker.workspaces.map((workspace) =>
        el("li", {}, [
          el("button", {
            text: workspace.display_name,
            onClick: () => guard(() => chooseWorkspace(workspace)),
          }),
        ]),
      ),
    );
    return section("Choose a workspace", [
      picker.workspaces.length === 0
        ? el("p", { className: "notice", text: "You're not a member of any workspace yet." })
        : list,
      el("div", { className: "row" }, [cancel]),
    ]);
  }

  if (picker.step === "drive") {
    const list = el("ul", { className: "picker" },
      picker.drives.map((drive) =>
        el("li", {}, [
          el("button", {
            text: drive.name,
            onClick: () => guard(() => chooseDrive(drive)),
          }),
        ]),
      ),
    );
    return section(`Choose a drive in ${picker.workspace.display_name}`, [
      picker.drives.length === 0
        ? el("p", {
            className: "notice",
            text: "This workspace has no drives you can write to yet. Create one in the Token Canopy console first.",
          })
        : list,
      el("div", { className: "row" }, [cancel]),
    ]);
  }

  // step === "folder"
  const trailRow = el("div", { className: "row crumbs" },
    picker.trail.flatMap((entry, index) => [
      index > 0 ? el("span", { text: "›" }) : null,
      el("button", {
        className: "link",
        text: entry.name,
        onClick: () => guard(() => browseTo(index)),
      }),
    ]),
  );

  const children = picker.children.length === 0
    ? el("p", { className: "notice", text: "No sub-folders here." })
    : el("ul", { className: "picker" },
        picker.children.map((folder) =>
          el("li", {}, [
            el("button", {
              text: folder.name,
              onClick: () => guard(() => browseInto(folder)),
            }),
          ]),
        ),
      );

  const nameInput = el("input", { type: "text", placeholder: "New folder name" });
  const createRow = el("div", { className: "row" }, [
    nameInput,
    el("button", {
      className: "secondary",
      text: "Create folder here",
      onClick: () => {
        const name = nameInput.value.trim();
        if (name) guard(() => createFolderHere(name));
      },
    }),
  ]);

  return section("Choose a folder", [
    trailRow,
    children,
    createRow,
    el("div", { className: "row" }, [
      el("button", {
        text: "Save captures here",
        onClick: () => guard(() => saveCurrentFolder()),
      }),
      cancel,
    ]),
  ]);
}

function renderLocation() {
  const { location } = view.settings;
  const chooseButton = el("button", {
    text: location ? "Change location" : "Choose where captures go",
    onClick: () => guard(() => openPicker()),
  });

  if (!location) {
    return section("Where captures go", [
      el("p", {
        className: "lede",
        text: "SnipIt doesn't have a place to save captures yet. Choose one, or let it suggest one if you have a single drive.",
      }),
      el("div", { className: "row" }, [
        chooseButton,
        el("button", {
          className: "secondary",
          text: "Suggest one",
          onClick: () =>
            guard(async () => {
              const proposal = await proposeFirstRun();
              if (!proposal) {
                throw new Error(
                  "You have more than one workspace or drive, so there's no safe guess — pick where captures should go.",
                );
              }
              await acceptProposal(proposal);
            }),
        }),
      ]),
    ]);
  }

  return section("Where captures go", [
    location.stale
      ? el("div", {
          className: "error",
          text: `SnipIt can't reach this location any more — it may have been deleted, or your access removed.`,
        })
      : null,
    el("div", { className: "card crumbs", text: crumbs(location) }),
    el("div", { className: "row" }, [chooseButton]),
  ]);
}

function preferenceRow(label, key, options) {
  const select = el("select", {
    onChange: (event) => {
      const raw = options.find((option) => String(option.value) === event.target.value);
      guard(() => updatePreferences({ [key]: raw.value }));
    },
  }, options.map((option) =>
    el("option", { value: String(option.value), text: option.label }),
  ));
  select.value = String(view.settings.preferences[key]);
  return el("label", { className: "field" }, [
    el("span", { text: label }),
    select,
  ]);
}

function renderPreferences() {
  const preferences = view.settings.preferences ?? DEFAULT_PREFERENCES;
  return section("After a capture", [
    preferenceRow("Group captures into date folders", "group_by_date", [
      { value: true, label: "Yes — one folder per day" },
      { value: false, label: "No — all in one folder" },
    ]),
    preferenceRow("Copy a link to the clipboard", "link", [
      { value: "share", label: "A share link anyone can open" },
      { value: "console", label: "A console link for workspace members" },
      { value: "none", label: "Don't copy a link" },
    ]),
    preferences.link === "share"
      ? preferenceRow("Share links expire", "link_expiry_days", [
          { value: null, label: "Never" },
          { value: 7, label: "After 7 days" },
          { value: 30, label: "After 30 days" },
          { value: 90, label: "After 90 days" },
        ])
      : null,
    preferenceRow("Open the capture in the console", "open_console", [
      { value: false, label: "No" },
      { value: true, label: "Yes — open a tab" },
    ]),
    preferenceRow("Saved page address", "strip_query", [
      { value: false, label: "Keep the full address" },
      { value: true, label: "Strip the query string" },
    ]),
    el("p", {
      className: "hint",
      text: "SnipIt saves the address of the page you captured alongside the image, so you can get back to it.",
    }),
  ]);
}

function renderDeveloper() {
  // Unpacked installs only. A packaged build ignores the override, so
  // offering the control there would be a lie.
  if (view.endpoints.env === "production") return null;

  const fields = ["hubBase", "apiBase", "consoleBase", "shareBase"];
  const inputs = {};
  const rows = fields.map((field) => {
    const input = el("input", { type: "text", value: view.endpoints[field] });
    input.value = view.endpoints[field];
    inputs[field] = input;
    return el("label", { className: "field" }, [
      el("span", { text: field }),
      input,
    ]);
  });

  return section("Developer", [
    el("p", {
      className: "hint",
      text: "This unpacked build talks to staging by default. Point it at a local stack here; a packaged build always uses production and ignores this.",
    }),
    ...rows,
    el("div", { className: "row" }, [
      el("button", {
        className: "secondary",
        text: "Use these endpoints",
        onClick: () =>
          guard(async () => {
            const next = {};
            for (const field of fields) next[field] = inputs[field].value.trim();
            await setEndpointOverride(next);
            // Credentials belong to the environment that issued them, and
            // so do the ids in the saved location.
            await signOut();
            await clearDriveTokens();
            await clearLocation();
          }),
      }),
      el("button", {
        className: "secondary",
        text: "Reset to staging",
        onClick: () =>
          guard(async () => {
            await clearEndpointOverride();
            await signOut();
            await clearDriveTokens();
            await clearLocation();
          }),
      }),
    ]),
  ]);
}

function render() {
  clear(main);
  envLabel.textContent = view.endpoints ? view.endpoints.env : "";

  if (view.loading) {
    main.appendChild(el("p", { className: "lede", text: "Loading…" }));
    return;
  }

  if (view.error) {
    main.appendChild(el("div", { className: "error", text: view.error }));
  }

  if (!view.signedIn) {
    main.appendChild(
      section("Token Canopy", [
        el("p", {
          className: "lede",
          text: "Sign in to choose where SnipIt saves your captures.",
        }),
        el("div", { className: "row" }, [
          el("button", { text: "Sign in", onClick: () => guard(() => signIn()) }),
        ]),
      ]),
    );
    const developer = renderDeveloper();
    if (developer) {
      main.appendChild(el("div", { className: "divider" }));
      main.appendChild(developer);
    }
    return;
  }

  if (view.picker) {
    main.appendChild(renderPicker());
    return;
  }

  main.appendChild(renderLocation());
  main.appendChild(el("div", { className: "divider" }));
  main.appendChild(renderPreferences());

  const developer = renderDeveloper();
  if (developer) {
    main.appendChild(el("div", { className: "divider" }));
    main.appendChild(developer);
  }

  main.appendChild(el("div", { className: "divider" }));
  main.appendChild(
    section("Account", [
      el("div", { className: "row" }, [
        el("button", {
          className: "danger",
          text: "Sign out",
          onClick: () =>
            guard(async () => {
              await signOut();
              await clearDriveTokens();
              await clearLocation();
            }),
        }),
      ]),
    ]),
  );
}

reload();
