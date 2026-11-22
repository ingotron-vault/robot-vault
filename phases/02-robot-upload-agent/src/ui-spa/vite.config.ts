import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/",
  plugins: [react()],
  optimizeDeps: { exclude: ["@robot-vault/ui-kit"] },
  build: {
    // IN-TREE output: the robot container bind-mounts the repo, so an
    // image-baked dist directory would never be visible to it. Building
    // straight into the Python package's ui/ dir is what web.py's
    // StaticFiles mount (see create_app) actually serves.
    outDir: "../robot_upload_agent/ui",
    emptyOutDir: true,
    // The production bundle is served by the robot agent itself. Shipping a
    // source map exposes source and more than doubles the static payload;
    // developers still get normal source mapping from Vite's dev server.
    sourcemap: false,
  },
  server: {
    host: true,
    port: 5174,
    proxy: {
      "/api": {
        target: "http://localhost:8090",
        changeOrigin: true,
      },
      "/metrics": {
        target: "http://localhost:8090",
        changeOrigin: true,
      },
    },
  },
});
