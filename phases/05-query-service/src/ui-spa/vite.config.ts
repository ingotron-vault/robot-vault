import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: "http://robot-vault-query:8003",
        changeOrigin: true,
      },
      // Dev-token bootstrap for explicitly reported dev auth mode (see
      // lib/auth.ts) -- without this, `GET /auth/dev/token`
      // hits the vite dev server itself (404) instead of the backend, so the
      // dev server proxies it the same way `/api` already is.
      "/auth": {
        target: "http://robot-vault-query:8003",
        changeOrigin: true,
      },
      "/obs": {
        target: "http://robot-vault-query:8003",
        changeOrigin: true,
      },
    },
  },
});
