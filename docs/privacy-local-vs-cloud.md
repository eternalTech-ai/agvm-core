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

Detwin Cloud is a separate platform surface. It can provide Hosted MCP, managed
storage, billing, usage metering and advanced cloud modules.

Hosted mode must use tenant/workspace scoping, server-side authorization and
clear data residency rules. It is not implied by the local open-core install.

## Sync Model

For this release, Pro modules run in Detwin Cloud. That does not automatically
upload local brains. Local-to-cloud sync must be an explicit user action or a
clearly configured hosted mode.

## Backups And Exports

Brain export files can contain sensitive memory. Treat them like private data:
store them encrypted when possible, avoid sharing them in issue reports and
never commit them to Git.
