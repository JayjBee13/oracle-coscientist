import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    // Vite answers 403 "Blocked request" to any Host it was not told about, so a
    // reverse proxy that forwards the real hostname needs it listed here.
    allowedHosts: ["host.docker.internal"],
    proxy: {
      // Lets the browser call the API on a RELATIVE path. Remote viewers reach
      // the app through the gateway, where an absolute http://127.0.0.1:8787
      // base URL would resolve to the VIEWER's own machine and fail every call;
      // a relative /api is proxied by whoever served the page. This entry is
      // what keeps that relative path working for a browser pointed straight at
      // the dev server, where there is no gateway in front to do it.
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: false },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
    clearMocks: true,
  },
});
