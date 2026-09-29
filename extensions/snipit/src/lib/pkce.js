// PKCE (RFC 7636) S256, plus the `state` that binds a callback to the
// request that started it.
//
// Both secrets come from `crypto.getRandomValues`. 32 bytes base64url is 43
// characters, comfortably inside the spec's 43–128 range for a verifier.

const VERIFIER_BYTES = 32;
const STATE_BYTES = 32;

function base64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomBase64url(byteLength) {
  return base64url(crypto.getRandomValues(new Uint8Array(byteLength)));
}

/**
 * One PKCE attempt.
 *
 * The `verifier` is the secret: it stays in session storage and is sent
 * ONLY on the code exchange. The `challenge` is what rides the authorize
 * URL, and `state` is what the callback must echo back unchanged.
 *
 * @returns {Promise<{verifier: string, challenge: string, state: string}>}
 */
export async function createAttempt() {
  const verifier = randomBase64url(VERIFIER_BYTES);
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  return {
    verifier,
    challenge: base64url(new Uint8Array(digest)),
    state: randomBase64url(STATE_BYTES),
  };
}
