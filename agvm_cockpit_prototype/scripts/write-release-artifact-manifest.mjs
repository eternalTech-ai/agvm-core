#!/usr/bin/env node

// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
// SPDX-FileContributor: Lorenzo Massaro
// SPDX-License-Identifier: AGPL-3.0-only

import { createHash } from "node:crypto";
import { mkdir, readdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";

function option(name, fallback = "") {
  const index = process.argv.indexOf(`--${name}`);
  if (index < 0 || index + 1 >= process.argv.length) return fallback;
  return String(process.argv[index + 1]).trim();
}

const root = path.resolve(option("root"));
const output = path.resolve(option("output", path.join(root, ".well-known/detwin-artifact-manifest.json")));
const versionOutput = path.resolve(option("version-output", path.join(root, "version.json")));
const component = option("component");
const version = option("version", "0.0.0-dev");
const revision = option("revision", "unknown");
const sourceSha = option("source-sha", revision);
const releaseBundle = option("release-bundle");
const created = option("created", "1970-01-01T00:00:00Z");

if (!component) throw new Error("--component is required");
if (!(await stat(root)).isDirectory()) throw new Error("--root must be a directory");
const fullGitSha = /^[0-9a-f]{40}$/;
if (revision !== "unknown" && !fullGitSha.test(revision)) throw new Error("--revision must be a full 40-character Git SHA");
if (releaseBundle && (!fullGitSha.test(sourceSha) || revision !== sourceSha)) {
  throw new Error("release provenance requires matching full --revision and --source-sha values");
}

const versionPayload = {
  schema_version: "detwin.runtime.release.v1",
  component,
  version,
  revision,
  source_sha: sourceSha,
  release_bundle: releaseBundle,
  created,
};
await mkdir(path.dirname(versionOutput), { recursive: true });
await writeFile(versionOutput, `${JSON.stringify(versionPayload, null, 2)}\n`, { encoding: "utf8", mode: 0o644 });

async function filesBelow(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await filesBelow(absolute)));
    if (entry.isFile() && path.resolve(absolute) !== output) files.push(absolute);
  }
  return files;
}

const artifacts = [];
for (const absolute of await filesBelow(root)) {
  const bytes = await readFile(absolute);
  artifacts.push({
    path: path.relative(root, absolute).split(path.sep).join("/"),
    sha256: createHash("sha256").update(bytes).digest("hex"),
    size: bytes.length,
  });
}
artifacts.sort((left, right) =>
  Buffer.compare(Buffer.from(left.path, "utf8"), Buffer.from(right.path, "utf8")),
);

const aggregate = createHash("sha256");
for (const artifact of artifacts) {
  aggregate.update(`${artifact.path}\0${artifact.sha256}\0${artifact.size}\n`);
}

const manifest = {
  schema_version: "detwin.container.artifact_manifest.v1",
  component,
  version,
  revision,
  source_sha: sourceSha,
  release_bundle: releaseBundle,
  created,
  artifact_count: artifacts.length,
  artifact_set_sha256: aggregate.digest("hex"),
  artifacts,
};
await mkdir(path.dirname(output), { recursive: true });
await writeFile(output, `${JSON.stringify(manifest, null, 2)}\n`, { encoding: "utf8", mode: 0o644 });
