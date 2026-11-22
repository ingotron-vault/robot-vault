import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup, configure } from "@testing-library/react";

// findBy*/waitFor default to 1 s. Under `make check` load the first render of a
// page with several mocked fetches can take longer, which failed
// "refreshes processing evidence in an open drawer" once while passing on
// every rerun. A longer wait only slows a test that is already failing.
configure({ asyncUtilTimeout: 3000 });

// vitest.config.ts doesn't set test.globals, so @testing-library/react's own
// auto-cleanup (which only registers if it finds a *global* afterEach) never
// fires -- without this, every test's render() stays mounted in the shared
// jsdom document and piles up across tests in the same file.
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
});

// jsdom has no layout, so it ships no `scrollIntoView` -- and the Episodes
// detail panel calls it on open (deliberately, and deliberately non-smooth;
// see the comment on that effect). Left unstubbed, every test that opens the
// panel dies on a TypeError that has nothing to do with what it is asserting.
// A no-op is the honest stand-in: a test that wants to assert the scroll can
// still spy on this same function.
const elementProto = Element.prototype as { scrollIntoView?: () => void };
if (typeof elementProto.scrollIntoView !== "function") {
  elementProto.scrollIntoView = () => {};
}
