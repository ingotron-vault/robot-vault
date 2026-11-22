import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// vitest.config.ts doesn't set test.globals, so @testing-library/react's own
// auto-cleanup (which only registers if it finds a *global* afterEach) never
// fires -- without this, every test's render() stays mounted in the shared
// jsdom document and piles up across tests in the same file.
afterEach(() => {
  cleanup();
  localStorage.clear();
});
