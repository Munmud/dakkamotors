import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In production CloudFront serves the app and the API from one origin, so the project
// has no CORS configuration at all. The dev proxy reproduces that arrangement locally:
// the browser only ever talks to the Vite origin, and /api is forwarded to Django.
const proxy = {
  "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
  "/media": { target: "http://127.0.0.1:8000", changeOrigin: true },
};

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  // `preview` gets the same proxy, which `server` does not hand it. Without this the
  // built bundle can only be run against nothing, and the dev server is the wrong
  // thing to measure: it ships every module unbundled and unminified, which reads as
  // 33 requests and 5.4MB of JavaScript that production never sends.
  preview: { proxy },
});
