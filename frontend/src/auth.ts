const clientId = "flight-web";
const scopes = [
  "openid",
  "profile",
  "email",
  "flights:read",
  "tickets:read",
  "tickets:write",
  "loyalty:read",
  "loyalty:write",
  "payments:read",
  "payments:write",
  "stats:read"
].join(" ");

const base64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

const random = (length = 32) => base64url(crypto.getRandomValues(new Uint8Array(length)));

const redirectUri = () => `${window.location.origin}/callback`;

export async function beginLogin(): Promise<void> {
  const verifier = random(48);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const state = random();
  const nonce = random();
  sessionStorage.setItem("pkce_verifier", verifier);
  sessionStorage.setItem("oauth_state", state);
  sessionStorage.setItem("oauth_nonce", nonce);
  const query = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri(),
    response_type: "code",
    scope: scopes,
    state,
    nonce,
    code_challenge: base64url(new Uint8Array(digest)),
    code_challenge_method: "S256"
  });
  window.location.assign(`/oauth2/authorize?${query}`);
}

export async function finishLogin(): Promise<void> {
  const query = new URLSearchParams(window.location.search);
  const error = query.get("error");
  if (error) throw new Error(query.get("error_description") || error);
  const code = query.get("code");
  const state = query.get("state");
  const verifier = sessionStorage.getItem("pkce_verifier");
  if (!code || !verifier || state !== sessionStorage.getItem("oauth_state")) {
    throw new Error("Не удалось проверить ответ Identity Provider");
  }
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: clientId,
    code,
    redirect_uri: redirectUri(),
    code_verifier: verifier
  });
  const response = await fetch("/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body
  });
  if (!response.ok) throw new Error("Identity Provider отклонил код авторизации");
  const tokens = await response.json();
  sessionStorage.setItem("access_token", tokens.access_token);
  sessionStorage.setItem("refresh_token", tokens.refresh_token);
  sessionStorage.setItem("id_token", tokens.id_token);
  for (const key of ["pkce_verifier", "oauth_state", "oauth_nonce"]) sessionStorage.removeItem(key);
  window.history.replaceState({}, "", "/");
}

export const accessToken = () => sessionStorage.getItem("access_token");

export function logout(): void {
  sessionStorage.clear();
  window.location.assign("/");
}

export type Session = {
  sub: string;
  name: string;
  preferred_username: string;
  role: string;
};

export function session(): Session | null {
  const raw = sessionStorage.getItem("id_token");
  if (!raw) return null;
  try {
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(raw.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0))));
  } catch {
    return null;
  }
}

