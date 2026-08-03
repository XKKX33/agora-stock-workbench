export class ApiError extends Error {
  constructor(status, message, details = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

export async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || 30000);
  try {
    const response = await fetch(path, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(
        response.status,
        payload?.error?.message || payload?.detail?.[0]?.msg || "请求失败",
        payload?.error?.details || {},
      );
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new ApiError(408, "请求超时");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export function query(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  });
  return request(`${url.pathname}${url.search}`);
}
