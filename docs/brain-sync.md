<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Brain Sync

AGVM Core is local-first. Brain Sync is an explicit Detwin Cloud workflow, not an
automatic side effect of signing in, running MCP or opening the local UI.

## Local Core Boundary

The public Core repository may provide export/import helpers and UI links to
Detwin Cloud. It must not include tenant storage, billing logic, cloud
materialization workers or private sync infrastructure.

## Cloud Workflow

The Detwin platform owns the metered sync flow:

1. Preview direction and estimated cost.
2. Confirm the selected local and cloud brain.
3. Reserve credits before upload or materialization.
4. Upload/import and materialize server-side.
5. Record a receipt in the usage ledger.

If credits are insufficient, the platform must block before any merge or
materialization begins.

## Local Workflow

Local AGVM should make the boundary clear:

- create or select a local brain;
- export/import local brain data intentionally;
- open Detwin Cloud only when the user chooses to sync;
- never upload a local brain automatically on login.

## Support Notes

When reporting sync problems, share only receipts, operation IDs and sanitized
counts. Do not post raw local brain exports in public issues.
