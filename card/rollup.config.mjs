import { readFileSync } from "node:fs";

import resolve from "@rollup/plugin-node-resolve";
import terser from "@rollup/plugin-terser";
import typescript from "@rollup/plugin-typescript";

// The card shares the integration version (single source: manifest.json)
const { version } = JSON.parse(readFileSync("../custom_components/host_agent/manifest.json", "utf8"));

const injectVersion = {
  name: "inject-version",
  transform: (code) => ({ code: code.replaceAll("__VERSION__", version), map: null }),
};

// Built into the integration, which serves it (never committed: CI builds it at release)
export default {
  input: "src/host-agent-card.ts",
  output: {
    file: "../custom_components/host_agent/frontend/host-agent-card.js",
    format: "es",
    sourcemap: false,
  },
  plugins: [
    injectVersion,
    resolve(),
    typescript({ noEmit: false, declaration: false }),
    terser({ format: { comments: false } }),
  ],
};
