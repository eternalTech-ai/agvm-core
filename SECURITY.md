# Security Policy

## Supported Versions

AGVM Core follows the security policy for the latest public `main` branch and
tagged releases.

## Reporting A Vulnerability

Report issues privately to the repository owner. Do not open public issues for
suspected secrets, authentication bypasses, data exposure or arbitrary code
execution.

## Local Threat Model

AGVM Core is local-first. Local brains, provider keys and runtime data should
remain on the user's machine unless the user explicitly connects to a hosted
AGVM platform.

The browser UI must not store raw provider keys in localStorage. Secrets belong
in backend-managed environment storage or an OS/platform secret manager.

## MCP Permissions

MCP tools are grouped by permission family:

- `read_only`: safe recall and inspection;
- `read_only_export`: larger read/export payloads;
- `registry_write`: brain registry create/select/ensure operations;
- `preview_only`: memory formation or maintenance previews without mutation;
- `explicit_apply`: mutation tools that require explicit apply;
- `destructive`: destructive admin operations, blocked by default.

Production clients should start with the smallest permission family set that
supports the workflow.

## Public/Private Boundary

Paid modules and cloud platform code are not part of AGVM Core. For this
release, advanced modules are account-gated in Detwin Cloud. UI hiding is not a
security boundary.

For the full public model, see [Security Model](docs/security-model.md).
