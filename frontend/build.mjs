// Deterministic esbuild build of the v2 client app.
//
// Bundles src/main.tsx (Preact, TS) into a single self-contained IIFE and extracts
// the imported CSS into a sibling stylesheet. Output goes to the committed asset dir
// (../src/metsuke/dashboard2/assets) as app.js + app.css — those files are what ship;
// end users never run node, only a developer rebuilding the frontend does.
//
// CSP note: format "iife" + no external imports means the bundle is same-origin
// script-src 'self'; there is zero inline script and zero runtime style injection.
import * as esbuild from "esbuild";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const outdir = resolve(here, "..", "src", "metsuke", "dashboard2", "assets");

await esbuild.build({
  entryPoints: [resolve(here, "src", "main.tsx")],
  bundle: true,
  format: "iife",
  target: ["es2020"],
  jsx: "automatic",
  jsxImportSource: "preact",
  minify: true,
  sourcemap: false,
  legalComments: "none",
  charset: "utf8",
  loader: { ".css": "css" },
  outdir,
  entryNames: "app",
  logLevel: "info",
});

const missing = ["app.js", "app.css"].filter((name) => !existsSync(resolve(outdir, name)));
if (missing.length > 0) {
  console.error("build did not produce:", missing.join(", "));
  process.exit(1);
}
console.log("built app.js + app.css ->", outdir);
