const TOKEN_BYTES = 32;
const TOKEN_CHARS = 43;
const TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;

export function generateAccessToken(randomSource: Crypto = globalThis.crypto): string {
  if (!randomSource || typeof randomSource.getRandomValues !== "function") {
    throw new Error("A secure random source is required.");
  }
  const bytes = new Uint8Array(TOKEN_BYTES);
  randomSource.getRandomValues(bytes);
  return encodeBase64Url(bytes);
}

export function isCanonicalAccessToken(token: unknown): token is string {
  if (typeof token !== "string" || TOKEN_RE.exec(token) === null) {
    return false;
  }
  try {
    const raw = decodeBase64Url(token);
    if (raw.length !== TOKEN_BYTES) {
      return false;
    }
    return encodeBase64Url(raw) === token;
  } catch {
    return false;
  }
}

function encodeBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  const encoded = globalThis.btoa(binary);
  const url = encoded.replaceAll("+", "-").replaceAll("/", "_");
  const trimmed = url.replace(/=+$/u, "");
  if (trimmed.length !== TOKEN_CHARS) {
    throw new Error("Access token encoding produced an unexpected length.");
  }
  return trimmed;
}

function decodeBase64Url(token: string): Uint8Array {
  const padded = token.replaceAll("-", "+").replaceAll("_", "/") + "=";
  const binary = globalThis.atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}
