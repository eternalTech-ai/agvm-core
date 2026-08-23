<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Security Model

AGVM Core uses a layered security model. The public repository must be safe to
clone and inspect without exposing paid code, secrets, local brains or private
cloud infrastructure.

## Trust Boundaries

- Browser UI: not trusted for entitlement enforcement or secret storage.
- Local API: owns local runtime state, backend-managed environment values and
  public Core permission checks.
- MCP bridge: adapts an AI client to AGVM tools and enforces visible tool
  filtering based on permission families.
- Hosted platform: owns account login, billing, tenant scoping, cloud module
  entitlements and usage metering.
- Paid modules: run in Detwin Cloud for this release and must refuse paid
  capabilities unless server-side account, plan, brain, provider and credit
  checks pass.

Brain Profile activation, paid fitting and paid backfill follow the same
server-authorized boundary. The public profile schema and preview contract do
not grant entitlement. A local process without authority receives a structured
Cloud action contract and must not simulate activation.

## Secrets

Provider keys belong in backend-managed environment storage, `.env` files that
are never committed, or an OS/platform secret manager. The UI must not write raw
provider keys to localStorage.

Public docs and examples must use empty or placeholder values only. Export scans
must reject token-like strings, local user paths and private key blocks.

## MCP Permission Families

AGVM MCP tools are grouped into permission families:

- `read_only`: safe recall and inspection;
- `read_only_export`: larger read/export payloads;
- `registry_write`: brain create/select/ensure operations;
- `preview_only`: preview tools that do not mutate memory;
- `explicit_apply`: mutation tools that require an explicit apply call;
- `destructive`: destructive admin operations, blocked by default.

Normal local memory usage should allow `explicit_apply` only when the user wants
the AI client to persist approved memory operations. `destructive` should stay
blocked unless the operator intentionally enters an admin workflow.

## Module Security

Paid module code is not shipped in the public core repository. Advanced modules
are Detwin Cloud capabilities for this release. Detwin Cloud must enforce plan,
workspace, provider, brain and credit checks server-side. UI hiding is not a
security boundary.

## Data Safety

Local brains can contain sensitive memory. Treat brain exports, SQLite files,
runtime logs and source packages as private data. Do not attach them to public
issues unless they are sanitized.

## Release Gate

A public export must pass:

- allowlist-based export from the private repository;
- denylist checks for private paths and internal AGVM docs;
- secret scans for API keys, token-like strings and local machine paths;
- a clean new Git history for the public repository;
- legal review of license, trademarks, terms and contribution policy before
  public launch.
