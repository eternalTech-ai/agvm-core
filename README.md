<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# AGVM Core

AGVM Core is a local-first memory operating system for MCP clients. It gives an
AI client a persistent memory brain, a self-hosted API, a local stdio MCP bridge
and a core UI for setup, inspection, health checks, benchmarks and raw MCP calls.

This repository is the open-source core. It is deliberately smaller than the
private AGVM lab repository: paid modules, cloud control-plane code, private AWS
infrastructure, internal planning documents, local brains and generated
benchmark artifacts are not included.

## What AGVM Core Is

- a local runtime for AGVM memory brains;
- a brain registry and scoped runtime storage;
- public MCP contracts for local AI clients;
- a Docker-based API and UI package;
- setup surfaces for MCP client configuration;
- explicit local brain bootstrap, create/import and switching workflows;
- Retrieve, Health, Bench and MCP Raw Console surfaces when the public core API
  exports their backing endpoints.

## What AGVM Core Is Not

- not the Cloud Clone, Teach, Maintain or advanced chat product;
- not the hosted AGVM cloud platform;
- not a billing, entitlement or private module registry;
- not a place to commit provider keys, local brains, customer data or old
  private AGVM strategy docs.

## Quickstart

```bash
cp .env.example .env
docker compose up --build
```

Then open:

- UI: `http://localhost:3020`
- API health: `http://localhost:8010/health`
- API docs: `http://localhost:8010/docs`

No provider key is required to start AGVM Core, create a brain, begin the manual
Bootstrap flow or connect MCP. When provider-backed operations are needed, open
Local Settings, test the OpenAI key and save it there. The API stores it in the
local Docker data volume and applies it to the running process; no container
restart is required. The browser never writes the raw key to `localStorage`.
Headless operators may still provide `OPENAI_API_KEY` through `.env` instead.

The saved key is local BYOK: provider usage is billed by the provider account
that owns the key, not by Detwin. AI interview generation and semantic candidate
formation during reviewed Brain Bootstrap, semantic Grow and Context/Search run
against this local provider and do not consume Detwin credits.

See [Local Install](docs/local-install.md), [Brain Bootstrap](docs/brain-bootstrap.md),
[Brain Profile V1](docs/brain-profile-v1.md) and [Brain Core](docs/brain-core.md)
for the setup, personalization and visualization boundaries.

## First Local Workflow

1. Run `docker compose up --build`, then open `http://localhost:3020`.
2. In Local Settings, test and save your provider key. Manual setup can start
   without it, but AI interview, Bootstrap semantic preview, semantic Grow and
   Context/Search fail closed until a provider is configured.
3. In Brain Center, create or select a local brain and start the **AI interview**
   for locally generated bounded questions. API and MCP callers may instead
   start a manual interview and supply the bounded questions themselves. Answer
   the interview, add reviewed source material, preview the candidates and
   explicitly apply the selected candidate IDs.
4. In Grow, preview a local source and explicitly apply the reviewed nodes.
5. In Context, run a query against the selected brain. MCP clients use
   `retrieve_context` and can inspect the returned `search_id` without starting
   another search.

These steps use Local Core and require no Detwin account or Detwin credits.
Sleep, Evolve and advanced calibration are separate cloud-backed Maintain
operations. Calling them through MCP requires a Hosted MCP key plus a valid
Detwin account, plan, cloud brain, provider readiness and available credits.

## Connect An MCP Client

Start the AGVM API first, then configure your AI app to launch the local stdio
bridge from this checkout. The generic flow is documented in [Local MCP](docs/local-mcp.md).

```bash
python -m agvm_mcp_server
```

- [Codex](docs/mcp-codex.md)
- [Claude](docs/mcp-claude.md)
- [Cursor](docs/mcp-cursor.md)

After the app restarts, ask it to call `get_agvm_usage_guide` and list the AGVM
tools it can see. That is the first connection check.

## Core UI

The public Docker UI boots the Local AGVM cockpit. It uses the same visual
system as Detwin Cloud, scoped to the AGPL Core boundary:

- Brain Center: create, import, switch and inspect local brains;
- Brain Core: inspect a real-node-only 3D projection of the selected brain;
- Context: retrieve from the selected local brain;
- Grow: add text, URL, website or uploaded source material through preview and
  explicit apply;
- Health and Bench: inspect local brain readiness and claim boundaries;
- MCP Setup: generate client configuration for Codex, Claude, Cursor or generic
  MCP clients;
- MCP Raw Console: inspect the visible catalog and call exposed MCP tools for
  debugging.

If a screen shows a disabled or missing capability, treat that as a product
boundary signal. Paid modules are not hidden in the public repository.

## Modules And Pro

Advanced modules are not distributed in the public Core repository. Grow remains
part of Local Core. Clone, Teach and Maintain are Detwin Cloud capabilities that
require platform account state, server-side entitlement checks and cloud usage
metering.

See [Modules](docs/modules.md) and [Cloud And Pro](docs/cloud-and-pro.md).

## Privacy And Security

- [Local Vs Cloud Privacy](docs/privacy-local-vs-cloud.md)
- [Security Model](docs/security-model.md)
- [Security Policy](SECURITY.md)
- [License And Notices](docs/license-and-notices.md)
- [Third-Party Notices](THIRD_PARTY_NOTICES.md)
- [Notice](NOTICE)
- [Support](SUPPORT.md)
- [Code Of Conduct](CODE_OF_CONDUCT.md)

Local mode keeps brains and provider configuration on the user's machine unless
the user explicitly exports data or connects to hosted AGVM.

## Contributing

Read [Contributing](CONTRIBUTING.md) before opening pull requests. Public
contributions must stay inside the core/runtime/docs boundary and must not add
paid module implementations or private platform code.
