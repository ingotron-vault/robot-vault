import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Same shape as the robot upload-agent SPA's vitest.config.ts, deliberately:
// two SPAs in one npm workspace root with two different test setups is a tax
// nobody wants to pay twice.
//
// `optimizeDeps.exclude` for the kit: `@robot-vault/ui-kit` is a source-only
// workspace package (its `main` is `src/index.ts`), so it must go through the
// same TSX pipeline as the app rather than esbuild's dependency pre-bundle.
export default defineConfig({
  plugins: [react()],
  optimizeDeps: { exclude: ["@robot-vault/ui-kit"] },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/__tests__/setup.ts"],
    css: false,
    // `make check` runs this suite while other containers build. Give slow
    // renders room: findBy* waits up to 3 s (setup.ts), so a test with several
    // waits needs more than vitest's 5 s default.
    testTimeout: 15000,
  },
});
