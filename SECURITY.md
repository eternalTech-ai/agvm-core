<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Security Policy

## Supported Versions

AGVM Core has not reached public release. Security support starts when the first
public version is tagged.

## Reporting A Vulnerability

Report issues privately through GitHub private vulnerability reporting when it
is enabled for the public repository. If that is unavailable, contact
`info@eternaltech.ai` and include only sanitized logs.

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

Paid modules and cloud platform code are not part of AGVM Core. Their activation
and entitlement checks must be enforced server-side by the platform and locally
by the module host. UI hiding is not a security boundary.

For the full public model, see [Security Model](docs/security-model.md).
