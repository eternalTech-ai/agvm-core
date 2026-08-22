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

With the default public Core configuration the bridge exposes 37 tools. Grow,
brain registry, retrieval, write-preview/commit and local graph inspection
tools are Core tools. Maintain-family module tools such as `sleep_preview`,
`sleep_apply`, `evolve_preview`, `evolve_apply`,
`matrix_calibration_preview`, `matrix_calibration_apply` and
`list_memory_os_processes` stay visible so AI clients can plan correctly, but a
direct call is blocked before API execution unless a valid local module lease is
present. The block response includes an action contract that points the user to
Detwin Cloud or account renewal.

## Tool Boundary

Local Core tools do not consume Detwin Cloud credits. Hosted MCP, Cloud AGVM and
advanced cloud modules are metered by the Detwin platform, not by the local
Core bridge.

Do not treat a visible paid-module MCP tool as authorization. The boundary is
the server-side call gate: missing, expired or insufficient module access must
return `module_tool_not_enabled_by_local_mcp_lease` and must not pass the call
through to the local API.

## Brain And Graph Export

Local brain export is explicit. Use `/mcp/brains/export` after selecting the
intended brain. `/memory/brains/export` remains a browser compatibility alias, not
the primary MCP contract. The archive
contains the brain record, storage files and an export manifest with graph
summary metadata: runtime node count, graph payload node/edge counts and the
included graph/index/atlas/sqlite files. Treat every brain archive as sensitive
project data.
