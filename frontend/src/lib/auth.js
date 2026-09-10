/**
 * Customer session handling.
 *
 * The CSRF token deliberately comes from `/api/auth/csrf/` rather than from the page.
 * Pages are cached at the CDN for five minutes with cookies ignored, so a token set
 * during a page render would be handed to every visitor alike — which is exactly what
 * CSRF protection exists to prevent. Everything under `/api/` is uncached and forwards
 * cookies, so the token is fetched from there on demand.
 */

import { api } from "../api/client";

function cookie(name) {
  const match = document.cookie.match(new RegExp(`(^|;\\s*)${name}=([^;]+)`));
  return match ? decodeURIComponent(match[2]) : null;
}

async function csrfToken() {
  const existing = cookie("csrftoken");
  if (existing) return existing;
  const { data } = await api.get("/auth/csrf/");
  return data.csrfToken ?? cookie("csrftoken");
}

/** POST with the CSRF header Django requires for any session-authenticated write. */
async function post(path, body) {
  const token = await csrfToken();
  const { data } = await api.post(path, body, {
    headers: token ? { "X-CSRFToken": token } : {},
  });
  return data;
}

export async function register({ name, email, phone, password }) {
  return post("/auth/register/", { name, email, phone, password });
}

export async function login({ email, password }) {
  return post("/auth/login/", { email, password });
}

export async function logout() {
  return post("/auth/logout/", {});
}

/** The signed-in customer, or null. A 401/403 here is an answer, not an error. */
export async function fetchMe() {
  try {
    const { data } = await api.get("/auth/me/");
    return data;
  } catch {
    return null;
  }
}

export async function fetchSlots() {
  const { data } = await api.get("/test-drive/slots/");
  return data.results ?? [];
}

export async function fetchMyBookings() {
  const { data } = await api.get("/test-drive/bookings/");
  return data.results ?? [];
}

export async function bookSlot({ slot, car }) {
  return post("/test-drive/bookings/", { slot, car });
}

export async function cancelBooking(id) {
  return post(`/test-drive/bookings/${id}/cancel/`, {});
}

export async function rescheduleBooking(id, slot) {
  return post(`/test-drive/bookings/${id}/reschedule/`, { slot });
}

/** Turn a DRF error into something worth showing a customer. */
export function errorMessage(error, fallback) {
  const data = error?.response?.data;
  if (!data) return fallback;
  if (typeof data.detail === "string") return data.detail;
  // Field errors: surface the first, which is the one they need to fix.
  const first = Object.values(data)[0];
  if (Array.isArray(first) && first.length) return first[0];
  return fallback;
}
