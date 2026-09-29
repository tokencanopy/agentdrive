// The Hub calls the extension makes, beside the OAuth ones in hub-auth.js.
//
// Exists so `options.js` stops building raw `fetch`es: every other network
// call in the extension goes through a module that validates what comes
// back, and the picker — which decides where somebody's screenshots land —
// should not be the exception. It is also the last place a path lived
// outside `config.js`.

import { getEndpoints } from "./config.js";
import { hubToken } from "./hub-auth.js";

export class HubApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

/**
 * One workspace row, or `null`.
 *
 * Drops a malformed row rather than failing the page: a picker that renders
 * three of four workspaces is usable, and one that renders an error because
 * a display name came back as a number is not.
 */
function workspaceFrom(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const { id, display_name: displayName, role } = value;
  if (
    typeof id !== "string" ||
    id.length === 0 ||
    id.length > 128 ||
    typeof displayName !== "string" ||
    displayName.length > 512 ||
    typeof role !== "string" ||
    role.length > 64
  ) {
    return null;
  }
  // Exactly the three fields the picker reads. `kind` is a migration
  // artifact and anything else hub adds later has no reader here.
  return { id, display_name: displayName, role };
}

/**
 * `GET /v0/snipit/workspaces` — the workspaces this principal actively
 * belongs to, most-owned first.
 *
 * @param {{limit?: number, cursor?: string}} [options]
 * @returns {Promise<{items: Array<{id: string, display_name: string, role: string}>, nextCursor: string | null}>}
 */
export async function listWorkspaces({ limit = 100, cursor } = {}) {
  const bearer = await hubToken();
  const { hubBase } = await getEndpoints();
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) params.set("cursor", cursor);

  let response;
  try {
    response = await fetch(
      `${hubBase}/v0/snipit/workspaces?${params.toString()}`,
      {
        credentials: "omit",
        headers: { Authorization: `Bearer ${bearer}` },
      },
    );
  } catch {
    throw new HubApiError(
      "HUB_UNAVAILABLE",
      "Couldn't reach Token Canopy. Check your connection and try again.",
    );
  }

  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new HubApiError(
        "NOT_SIGNED_IN",
        "Your Token Canopy sign-in is no longer valid — sign in again.",
        response.status,
      );
    }
    throw new HubApiError(
      "HUB_UNAVAILABLE",
      `Couldn't list your workspaces (${response.status}).`,
      response.status,
    );
  }

  const body = await response.json().catch(() => null);
  const rows = Array.isArray(body?.items) ? body.items : [];
  return {
    items: rows.map(workspaceFrom).filter((row) => row !== null),
    nextCursor: typeof body?.next_cursor === "string" ? body.next_cursor : null,
  };
}
