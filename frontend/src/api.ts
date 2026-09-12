import { accessToken, logout } from "./auth";

export async function api<T>(path: string, options: RequestInit = {}, authenticated = false): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (authenticated) {
    const token = accessToken();
    if (!token) throw new Error("Войдите, чтобы продолжить");
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && authenticated) {
    logout();
    throw new Error("Сессия завершена. Войдите снова.");
  }
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try {
      message = (await response.json()).detail || message;
    } catch { /* response is not JSON */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

