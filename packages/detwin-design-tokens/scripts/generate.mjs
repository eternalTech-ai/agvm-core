// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
// SPDX-FileContributor: Lorenzo Massaro
// SPDX-License-Identifier: AGPL-3.0-only

import { createHash } from "node:crypto";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = path.join(packageRoot, "src", "tokens.json");
const generatedRoot = path.join(packageRoot, "src", "generated");
const checkOnly = process.argv.includes("--check");

const digest = (value) => createHash("sha256").update(value).digest("hex");
const sourceBytes = await readFile(sourcePath);
const source = JSON.parse(sourceBytes.toString("utf8"));

const visualProjectionMetadata = source.visualProjection?._meta;
const visualProjectionTokens = Object.fromEntries(
  Object.entries(source.visualProjection ?? {}).filter(([key]) => key !== "_meta"),
);
const tokenGroups = {
  ...Object.fromEntries(
    Object.entries(source).filter(([key]) =>
      ![
        "_spdx",
        "schema_version",
        "design_package_aggregate_sha256",
        "visualProjection",
      ].includes(key),
    ),
  ),
  visualProjection: visualProjectionTokens,
};

if (
  visualProjectionMetadata?.schema_version !== "detwin.visual_color_projection.v1"
  || visualProjectionMetadata?.semantic_boundary !== "display_only_never_e_or_g"
) {
  throw new Error("The visual color projection metadata is missing or unsupported");
}

const flatten = (value, prefix = []) =>
  Object.keys(value)
    .sort()
    .flatMap((key) => {
      const next = value[key];
      const parts = [...prefix, key];
      return typeof next === "object" && next !== null
        ? flatten(next, parts)
        : [[parts, String(next)]];
    });

const cssName = (parts) =>
  `--dt-${parts
    .join("-")
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .toLowerCase()}`;

const css = [
  // REUSE-IgnoreStart
  "/* SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai> */",
  "/* SPDX-FileContributor: Lorenzo Massaro */",
  "/* SPDX-License-Identifier: AGPL-3.0-only */",
  // REUSE-IgnoreEnd
  "/* Generated from src/tokens.json. Do not edit. */",
  "",
  ":root {",
  ...flatten(tokenGroups).map(([parts, value]) => `  ${cssName(parts)}: ${value};`),
  "  --dt-letter-spacing: 0;",
  "}",
  "",
].join("\n");

const ts = [
  // REUSE-IgnoreStart
  "// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>",
  "// SPDX-FileContributor: Lorenzo Massaro",
  "// SPDX-License-Identifier: AGPL-3.0-only",
  // REUSE-IgnoreEnd
  "// Generated from src/tokens.json. Do not edit.",
  "",
  `export const designPackageAggregateSha256 = ${JSON.stringify(source.design_package_aggregate_sha256)} as const;`,
  `export const visualColorProjectionMetadata = ${JSON.stringify(visualProjectionMetadata, null, 2)} as const;`,
  `export const tokens = ${JSON.stringify(tokenGroups, null, 2)} as const;`,
  "export type DetwinTokens = typeof tokens;",
  "",
].join("\n");

const manifest = `${JSON.stringify(
  {
    _spdx: source._spdx,
    schema_version: "detwin.design_token_manifest.v1",
    design_package_aggregate_sha256: source.design_package_aggregate_sha256,
    visual_color_projection: visualProjectionMetadata,
    source: { path: "src/tokens.json", sha256: digest(sourceBytes) },
    outputs: [
      { path: "src/generated/tokens.css", sha256: digest(css) },
      { path: "src/generated/tokens.ts", sha256: digest(ts) },
    ],
  },
  null,
  2,
)}\n`;

const outputs = new Map([
  [path.join(generatedRoot, "tokens.css"), css],
  [path.join(generatedRoot, "tokens.ts"), ts],
  [path.join(generatedRoot, "token-manifest.json"), manifest],
]);

if (checkOnly) {
  const mismatches = [];
  for (const [target, expected] of outputs) {
    const actual = await readFile(target, "utf8").catch(() => null);
    if (actual !== expected) mismatches.push(path.relative(packageRoot, target));
  }
  if (mismatches.length > 0) {
    throw new Error(`Generated token outputs are stale: ${mismatches.join(", ")}`);
  }
} else {
  await mkdir(generatedRoot, { recursive: true });
  await Promise.all([...outputs].map(([target, value]) => writeFile(target, value, "utf8")));
}
