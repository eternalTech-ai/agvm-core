<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Local Install

AGVM Core runs locally as a Docker Compose stack with:

- `agvm_core_api`: the local API and brain runtime;
- `agvm_core_ui`: the browser UI on port `3020`;
- `agvm_mcp`: an optional container profile for MCP bridge diagnostics.

Desktop AI clients normally launch the MCP bridge with `python -m
agvm_mcp_server` from the checkout. The Docker `agvm_mcp` service is useful for
packaging checks, but stdio clients still need their own local process.

## Requirements

- Docker Desktop or a compatible Docker Compose runtime;
- Python 3.11 or newer for desktop MCP clients that launch the local bridge;
- Node.js 20 or newer only when developing the UI outside Docker;
- an LLM provider key when provider-backed retrieval or memory formation is
  needed.

## Start The Local Stack

```bash
cp .env.example .env
docker compose up --build
```

Expected local URLs:

- API: `http://127.0.0.1:8010`
- UI: `http://127.0.0.1:3020`
- API docs: `http://127.0.0.1:8010/docs`

Check health:

```bash
curl http://127.0.0.1:8010/health
```

## Environment File

`.env.example` is safe to commit. `.env` is local-only and must never be
committed.

Minimum local values:

```bash
OPENAI_API_KEY=
AGVM_API_PORT=8010
AGVM_UI_PORT=3020
AGVM_DEFAULT_BRAIN_ID=default_brain
AGVM_MCP_BRAIN_POLICY=ai_create_if_missing
AGVM_MCP_READ_ONLY=false
AGVM_MCP_MODULE_VISIBILITY_POLICY=block_unlicensed
```

Provider keys can also be saved through the UI setup flow when the backend
supports managed environment storage. Raw provider keys must not be written to
browser localStorage.

## Data Location

Docker volumes hold local runtime data:

- `agvm_core_data`: API runtime data;
- `agvm_core_brains`: local brain registry and brain files.

Removing those volumes removes local AGVM state. Export or back up local brains
before deleting volumes.

## First MCP Check

After the API is healthy, configure one MCP client:

- [Local MCP overview](local-mcp.md)
- [Codex](mcp-codex.md)
- [Claude](mcp-claude.md)
- [Cursor](mcp-cursor.md)

Ask the client to call `get_agvm_usage_guide`. If the client cannot see that
tool, the MCP bridge did not start or cannot reach `AGVM_API_BASE_URL`.

## First Brain

If Context, Grow or MCP pages report that no active brain exists, create or
import one before retrying. See [Brain Bootstrap](brain-bootstrap.md).
