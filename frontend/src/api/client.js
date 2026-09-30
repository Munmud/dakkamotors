/**
 * Everything the site asks of the server, which is not much: two reads and one write.
 *
 * Plain `fetch`, not axios. Axios was ~15 KB gzipped of retry, interceptor and
 * transform machinery for four calls that need none of it, on a site whose whole job
 * is to load fast enough that somebody stays. The one thing it gave us that fetch does
 * not — throwing on a 4xx — is eight lines below.
 */

const BASE = import.meta.env.VITE_API_BASE_URL ?? "/api";

/** Thrown on any non-2xx, carrying whatever the server said. */
export class ApiError extends Error {
  constructor(status, data) {
    super(data?.detail || `Request failed (${status})`);
    this.status = status;
    this.data = data;
  }
}

async function request(path, { method = "GET", body, signal, headers } = {}) {
  const response = await fetch(`${BASE}${path}`, {
    method,
    signal,
    headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });

  // A 204 or an empty body is a valid answer; do not make callers guard for it.
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

export function fetchCars({ page = 1, status, signal } = {}) {
  // No `status` is the stock -- available, then reserved. "sold" is the shelf below it.
  const query = new URLSearchParams({ page: String(page) });
  if (status) query.set("status", status);
  return request(`/cars/?${query}`, { signal });
}

export function fetchCar(slug, { signal } = {}) {
  return request(`/cars/${slug}/`, { signal });
}

/* ------------------------------------------------------------------- the one write */

function cookie(name) {
  const match = document.cookie.match(new RegExp(`(^|;\\s*)${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : null;
}

/**
 * The CSRF token deliberately comes from `/api/auth/csrf/` rather than from the page.
 * Pages are cached at the CDN for five minutes with cookies ignored, so a token set
 * during a page render would be handed to every visitor alike — which is exactly what
 * CSRF protection exists to prevent. Everything under `/api/` is uncached and forwards
 * cookies, so the token is fetched from there on demand.
 */
async function csrfToken() {
  const existing = cookie("csrftoken");
  if (existing) return existing;
  const data = await request("/auth/csrf/");
  return data?.csrfToken ?? cookie("csrftoken");
}

/**
 * Leave a name and a number. The only thing this site collects.
 *
 * `car` is a slug when the lead came off a car page and absent when it came from the
 * find-me-something form. The server decides which it is; the caller just passes what
 * it has.
 */
export async function sendLead({ name, phone, email, details, car, language }) {
  const token = await csrfToken();
  return request("/requests/", {
    method: "POST",
    headers: token ? { "X-CSRFToken": token } : {},
    body: { name, phone, email, details, car, language },
  });
}

/** Turn a failure into something worth showing somebody. */
export function errorMessage(error, fallback) {
  if (error instanceof ApiError && typeof error.data?.detail === "string") {
    return error.data.detail;
  }
  return fallback;
}
