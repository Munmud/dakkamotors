import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In production CloudFront serves the app and the API from one origin, so the project
// has no CORS configuration at all. The dev proxy reproduces that arrangement locally:
// the browser only ever talks to the Vite origin, and /api is forwarded to Django.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/media": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
