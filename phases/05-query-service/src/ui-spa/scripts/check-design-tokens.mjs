import { readdir, readFile } from "node:fs/promises";
import { relative } from "node:path";
import { fileURLToPath } from "node:url";

const sourceRoot = new URL("../src/", import.meta.url);
const tokenSheet = new URL("styles/app.css", sourceRoot);

const requiredTokens = new Map([
  ["--rv-type-caption", ".75rem"],
  ["--rv-type-ui", ".8125rem"],
  ["--rv-type-body", ".875rem"],
  ["--rv-type-title-sm", "1rem"],
  ["--rv-type-title-md", "1.25rem"],
  ["--rv-type-metric", "1.5rem"],
  ["--rv-type-page", "clamp(2rem, 3vw, 2.5rem)"],
  ["--rv-leading-caption", "1.333"],
  ["--rv-leading-ui", "1.385"],
  ["--rv-leading-body", "1.429"],
  ["--rv-leading-title-sm", "1.375"],
  ["--rv-leading-title-md", "1.4"],
  ["--rv-leading-metric", "1.167"],
  ["--rv-leading-page", "1.1"],
  ["--rv-weight-regular", "400"],
  ["--rv-weight-medium", "500"],
  ["--rv-weight-semibold", "600"],
  ["--rv-tracking-normal", "0"],
  ["--rv-tracking-label", ".06em"],
  ["--rv-tracking-heading", "-.015em"],
  ["--rv-tracking-display", "-.035em"],
  ["--rv-space-0", "0"],
  ["--rv-space-1", ".25rem"],
  ["--rv-space-2", ".5rem"],
  ["--rv-space-3", ".75rem"],
  ["--rv-space-4", "1rem"],
  ["--rv-space-5", "1.25rem"],
  ["--rv-space-6", "1.5rem"],
  ["--rv-space-8", "2rem"],
  ["--rv-space-10", "2.5rem"],
  ["--rv-space-12", "3rem"],
  ["--rv-space-16", "4rem"],
]);

const requiredThemeContract = [
  ":root {",
  "--rv-bg: #f6f5f1;",
  "--rv-surface: #ffffff;",
  "--rv-text: #16181b;",
  "--rv-accent: #1f5fa8;",
  "--rv-control-border: #8a8d93;",
  "--rv-chart-series-1: #1f5fa8;",
  ".dark {",
  "--rv-bg: #0d0f12;",
  "--rv-surface: #14171b;",
  "--rv-text: #eceae4;",
  "--rv-accent: #9ccbff;",
];

const propertyRules = [
  ["font-size", /^var\(--rv-type-/],
  ["font-family", /^var\(--rv-font-/],
  ["font-weight", /^var\(--rv-weight-/],
  ["line-height", /^var\(--rv-leading-/],
  ["letter-spacing", /^var\(--rv-tracking-/],
  ["font", /^inherit$/],
];

async function findCssFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const url = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, directory);
      if (entry.isDirectory()) return findCssFiles(url);
      return entry.name.endsWith(".css") ? [url] : [];
    }),
  );
  return nested.flat();
}

async function findComponentFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const url = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, directory);
      if (entry.isDirectory()) return findComponentFiles(url);
      return /\.(?:ts|tsx)$/.test(entry.name) ? [url] : [];
    }),
  );
  return nested.flat();
}

const errors = [];
const tokenCss = await readFile(tokenSheet, "utf8");

for (const [token, expected] of requiredTokens) {
  const declaration = `${token}: ${expected};`;
  if (!tokenCss.includes(declaration)) {
    errors.push(`styles/app.css must define ${declaration}`);
  }
}

for (const declaration of requiredThemeContract) {
  if (!tokenCss.includes(declaration)) {
    errors.push(`styles/app.css must preserve the Orbit light/dark theme contract: ${declaration}`);
  }
}

for (const file of await findCssFiles(sourceRoot)) {
  const css = await readFile(file, "utf8");
  const name = relative(fileURLToPath(sourceRoot), fileURLToPath(file));

  if (name !== "styles/app.css" && /color-scheme\s*:\s*dark/.test(css)) {
    errors.push(`${name} must inherit the active Orbit color-scheme instead of forcing dark controls`);
  }

  for (const [property, allowed] of propertyRules) {
    const declaration = new RegExp(`${property}\\s*:\\s*([^;}\\n]+)`, "g");
    for (const match of css.matchAll(declaration)) {
      const declarationValue = match[1].trim();
      if (declarationValue.endsWith("!important")) {
        const line = css.slice(0, match.index).split("\n").length;
        errors.push(`${name}:${line} ${property} must not use !important`);
        continue;
      }
      const value = declarationValue;
      if (name === "styles/app.css" && property === "font-size" && value === "16px") continue;
      if (value === "inherit" || allowed.test(value)) continue;
      const line = css.slice(0, match.index).split("\n").length;
      errors.push(`${name}:${line} ${property} must use an Orbit semantic token; found ${value}`);
    }
  }
}

for (const file of await findComponentFiles(sourceRoot)) {
  const source = await readFile(file, "utf8");
  const name = relative(fileURLToPath(sourceRoot), fileURLToPath(file));

  for (const match of source.matchAll(/\btext-\[([^\]]+)\]/g)) {
    const line = source.slice(0, match.index).split("\n").length;
    errors.push(`${name}:${line} arbitrary text size ${match[0]} is not part of the Orbit scale`);
  }
  for (const match of source.matchAll(/\btext-(?:base|lg|xl|2xl|3xl|4xl)\b/g)) {
    const line = source.slice(0, match.index).split("\n").length;
    errors.push(`${name}:${line} ${match[0]} bypasses the Orbit semantic title and metric roles`);
  }
  for (const match of source.matchAll(/\bfontSize\s*:/g)) {
    const line = source.slice(0, match.index).split("\n").length;
    errors.push(`${name}:${line} inline fontSize is not part of the Orbit scale`);
  }
}

if (errors.length) {
  console.error("Orbit design-token check failed:\n");
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

console.log("Orbit design-token check passed.");
