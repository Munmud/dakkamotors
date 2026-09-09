import axios from "axios";

const baseURL = import.meta.env.VITE_API_BASE_URL ?? "/api";

export const api = axios.create({ baseURL, timeout: 15000 });

export function fetchCars({ page = 1, signal } = {}) {
  return api.get("/cars/", { params: { page }, signal }).then((r) => r.data);
}

export function fetchCar(id, { signal } = {}) {
  return api.get(`/cars/${id}/`, { signal }).then((r) => r.data);
}
