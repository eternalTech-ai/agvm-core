# Security Model

AGVM Core uses a layered security model. The public repository must be safe to
clone and inspect without exposing paid code, secrets, local brains or private
cloud infrastructure.

## Trust Boundaries

- Browser UI: not trusted for entitlement enforcement or secret storage.
- Local API: owns local runtime state and backend-managed environment values.
- MCP bridge: adapts an AI client to AGVM tools and enforces visible tool
  filtering based on permission families.
- Hosted platform: owns account login, billing, tenant scoping and Pro cloud
  entitlements.
- Paid modules: run in cloud for this release and are not shipped in public
  core.

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

Paid module code is not shipped in the public core repository. For this release,
advanced modules are account-gated in Detwin Cloud. Local paid module delivery
must remain hidden until its helper, verification and revocation path is fully
tested. UI hiding is not a security boundary.

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
