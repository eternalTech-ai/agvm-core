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
- setup surfaces for provider keys and MCP client configuration;
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

Set `OPENAI_API_KEY` in `.env` or through the UI setup flow when provider-backed
operations are needed. The browser UI must not store raw provider keys in
localStorage; keys belong in backend-managed environment storage or an OS secret
manager.

See [Local Install](docs/local-install.md), [Brain Bootstrap](docs/brain-bootstrap.md),
[Brain Profile V1](docs/brain-profile-v1.md) and [Brain Core](docs/brain-core.md)
for the setup, personalization and visualization boundaries.

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
