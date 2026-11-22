/** @type {import('tailwindcss').Config} */
module.exports = {
  presets: [require("@robot-vault/ui-kit/tailwind-preset.cjs")],
  content: ["./index.html", "./src/**/*.{ts,tsx}", "../../../ui-kit/src/**/*.{ts,tsx}"],
};
