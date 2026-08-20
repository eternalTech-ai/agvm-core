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
AGVM_MCP_MODULE_VISIBILITY_POLICY=hide_unlicensed
```

Use the client-specific guides for exact configuration:

- [Codex](mcp-codex.md)
- [Claude](mcp-claude.md)
- [Cursor](mcp-cursor.md)

## First Check

Ask the client to call `get_agvm_usage_guide` and then list the AGVM tools it
can see. If the tool is missing, check the process working directory, Python
environment and `AGVM_API_BASE_URL`.

## Tool Boundary

Local Core tools do not consume Detwin Cloud credits. Hosted MCP, Cloud AGVM and
advanced cloud modules are metered by the Detwin platform, not by the local
Core bridge.
