<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Local MCP

AGVM Core exposes a local stdio MCP bridge for desktop AI clients. The bridge
connects the client to the local AGVM API and the selected local brain.

## Start The Runtime

```bash
cp .env.example .env
docker compose up --build
```

Confirm the API is reachable:

```bash
curl http://127.0.0.1:8010/health
```

## Launch The Bridge

Desktop clients should run the bridge from the AGVM Core checkout:

```bash
python -m agvm_mcp_server
```

The bridge reads its process environment or `AGVM_MCP_CONFIG`; it does not load
the repository `.env` file by itself. Put the variables below in the desktop
client's MCP configuration, export them in the launching shell, or point
`AGVM_MCP_CONFIG` at a JSON file derived from `agvm_mcp_server/config.example.json`.

Minimum environment:

```bash
AGVM_API_BASE_URL=http://127.0.0.1:8010
AGVM_MCP_BRAIN_POLICY=ai_create_if_missing
AGVM_MCP_READ_ONLY=false
AGVM_MCP_MODULE_VISIBILITY_POLICY=block_unlicensed
```

For isolated fresh-user tests or per-project memory, set both runtime storage
roots before starting the API:

```bash
AGVM_LAB_DATA_DIR=/absolute/path/to/agvm-data
AGVM_BRAINS_DIR=/absolute/path/to/agvm-brains
```

`AGVM_LAB_DATA_DIR` stores runtime ledgers and local state. `AGVM_BRAINS_DIR`
stores the brain registry and graph files. Setting only one of them can leave
the bridge connected to an older local brain registry.

Use the client-specific guides for exact configuration:

- [Codex](mcp-codex.md)
- [Claude](mcp-claude.md)
- [Cursor](mcp-cursor.md)

## First Check

Ask the client to call `get_agvm_usage_guide` and then list the AGVM tools it
can see. If the tool is missing, check the process working directory, Python
environment and `AGVM_API_BASE_URL`.

The current tool catalog is generated at runtime from `GET /mcp/contracts`.
Do not hard-code a tool count in client setup: the catalog grows as bounded Core
and cloud-handoff contracts are released. `tools/list` is the runtime authority
because client permission policy can expose a filtered set. Grow,
brain registry, retrieval, write-preview/commit and local graph inspection
tools are Core tools. The registry also includes nine bounded
`brain_bootstrap_*` tools and three `brain_profile_*` tools. Maintain-family
module tools such as `sleep_preview`,
`sleep_apply`, `evolve_preview`, `evolve_apply`,
`geometry_calibration_preview`, `geometry_calibration_apply`,
`geometry_calibration_rollback` and
`list_memory_os_processes` stay visible so AI clients can plan correctly, but a
direct call requires a Detwin Hosted MCP key. Without one, the bridge returns
`detwin_cloud_auth_required` with an action contract. With one, the bridge can
send a certified paid operation to Hosted MCP; Platform checks the account,
plan, cloud brain, provider and credits before dispatch and settles dynamic
usage from the Core terminal receipt. The certified stdio to Hosted paths include
`sleep_preview`, `evolve_preview` and the canonical Geometry Calibration
preview/apply/rollback tools. Other paid tools stay discoverable but return a
structured unavailable response until their Hosted adapter passes the same
execution and metering gate. The legacy
`matrix_calibration_preview` and `matrix_calibration_apply` names remain visible
only as backward-compatible aliases; new clients must use Geometry Calibration.

Create the key in Detwin Account, then expose it only to the MCP process:

```bash
AGVM_HOSTED_MCP_API_KEY=<hosted-key>
AGVM_HOSTED_MCP_URL=https://mcp.detwin.ai
```

Do not write the key into the shareable JSON configuration. The target brain
must already exist in the Detwin workspace, normally after an explicit Brain
Sync. A retry with the same MCP request ID and arguments reuses the same
idempotency key and cannot settle usage twice.

## Tool Boundary

Local Core tools do not consume Detwin Cloud credits. Hosted MCP, Cloud AGVM and
advanced cloud modules are metered by the Detwin platform, not by the local
Core bridge.

Do not treat a visible paid-module MCP tool as authorization. The boundary is
the server-side Hosted MCP gate. Missing credentials, entitlement or credits
return structured recovery responses; paid operations never fall back to local
execution. Grow remains a local Core operation and does not consume Detwin
credits.

For a fresh local brain, the intended MCP order is:

1. `ensure_brain` and retain the returned `brain_id`;
2. complete the reviewed `brain_bootstrap_*` sequence, using the local BYOK
   provider for adaptive interview questions and semantic candidate formation;
3. call `grow_source_preview`, review its exact IDs, then
   `grow_source_apply` with explicit confirmation;
4. call `retrieve_context` and use its `search_id` with inspection tools.

Do not use Sleep or Evolve to finish local onboarding. Those calls cross the
Hosted MCP boundary and require a Detwin account, entitlement, cloud brain and
credits. If a local Bootstrap, Grow or `retrieve_context` call reports platform
credits as its requirement, verify that the client is connected to
`http://127.0.0.1:8010` and invoking the Core tool rather than a hosted/cloud
endpoint.

Brain Profile preview is shadow-only. Profile fitting, activation and rollback
authority are cloud-backed and must return an action contract when the local
process lacks the required Detwin capability. See
[Brain Profile V1](brain-profile-v1.md).

The public distribution contains no local Brain Profile benchmark/runtime or
Geometry Calibration apply/rollback implementation. Those tool names remain in
`tools/list`, but calls are either forwarded by the MCP bridge to Hosted MCP or
answered by a fail-closed public route with a Detwin Cloud `action_contract`.
Installing or forging a local lease cannot turn those public stubs into paid
execution code.

## Brain And Graph Export

Local brain export is explicit. Use `/mcp/brains/export` after selecting the
intended brain. `/memory/brains/export` remains a browser compatibility alias, not
the primary MCP contract. The archive
contains the brain record, storage files and an export manifest with graph
summary metadata: runtime node count, graph payload node/edge counts and the
included graph/index/atlas/sqlite files. Treat every brain archive as sensitive
project data.
