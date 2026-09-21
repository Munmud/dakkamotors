import axios from "axios";

const baseURL = import.meta.env.VITE_API_BASE_URL ?? "/api";

export const api = axios.create({ baseURL, timeout: 15000 });

export function fetchCars({ page = 1, status, signal } = {}) {
  // No `status` is the stock -- available, then reserved. "sold" is the shelf below it.
  const params = status ? { page, status } : { page };
  return api.get("/cars/", { params, signal }).then((r) => r.data);
}

export function fetchCar(id, { signal } = {}) {
  return api.get(`/cars/${id}/`, { signal }).then((r) => r.data);
}
