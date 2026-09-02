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
- an LLM provider key only when provider-backed retrieval or memory formation
  is needed; it is not required for startup or local setup.

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
AGVM_GROW_PREVIEW_BINDING_SECRET=
```

The public Docker UI can test and save a provider key from Local Settings. The
API persists it in `agvm_core_data/agvm_runtime.env`, updates the running
process immediately and never returns the raw value to the browser. No restart
is required. Raw provider keys are never written to browser `localStorage`.

This is BYOK for the local runtime. AI interview generation and semantic
candidate formation in reviewed Brain Bootstrap, semantic Grow and
Context/Search use the configured provider account; they do not reserve or
consume Detwin credits. Provider billing and quota still apply directly to the
account that owns the key.

`OPENAI_API_KEY` in `.env` remains an optional headless/managed deployment
override. If both are present, the key explicitly saved through Local Settings
is the active local value on subsequent starts.

When `AGVM_GROW_PREVIEW_BINDING_SECRET` is empty, first-run setup generates a
random key and stores it in the local API data volume. Set an explicit value of
at least 32 bytes only when the runtime must share that key across managed
instances; never commit it.

## Data Location

Docker volumes hold local runtime data:

- `agvm_core_data`: API runtime data;
- `agvm_core_brains`: local brain registry and brain files.

Removing those volumes removes local AGVM state. Export or back up local brains
before deleting volumes.

The published Compose stack binds API and UI to loopback by default. It is a
same-machine product; exposing it to a network requires a reviewed reverse proxy,
authentication and TLS configuration that are outside this quickstart.

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

For a new brain, complete this order in the UI:

1. create or select the brain in Brain Center;
2. configure Local Settings before choosing the AI interview;
3. answer the bounded interview and add reviewed source material;
4. preview and explicitly apply the Bootstrap candidates;
5. preview/apply one Grow source;
6. run one Context query and inspect its returned search result.

Sleep and Evolve are not part of this local acceptance path. They are
cloud-backed Maintain tools and require a Detwin account, entitlement, cloud
brain and credits through Hosted MCP.
