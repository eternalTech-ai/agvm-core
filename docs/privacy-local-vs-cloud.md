<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Local Vs Cloud Privacy

AGVM Core is local-first. A local brain stays on the user's machine unless the
user explicitly exports it or connects to a hosted AGVM platform.

## Local Mode

- brain data is stored in local runtime storage;
- provider keys are configured locally;
- MCP clients call the local stdio bridge;
- no cloud AGVM account is required for the free core.
- raw provider keys must not be stored in browser localStorage.

## Hosted Mode

Hosted AGVM is a separate platform surface. It can provide hosted MCP, managed
storage, billing, usage metering and cloud module entitlements.

Hosted mode must use tenant/workspace scoping, server-side authorization and
clear data residency rules. It is not implied by the local open-core install.

## Sync Model

Detwin Pro unlocks advanced modules in Cloud AGVM. It does not automatically
upload local brains. Local-to-cloud sync must be an explicit user action with a
cost preview, user confirmation, credit reservation and terminal receipt.

## Backups And Exports

Brain export files can contain sensitive memory. Treat them like private data:
store them encrypted when possible, avoid sharing them in issue reports and
never commit them to Git.
