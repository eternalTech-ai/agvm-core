# AGVM Core

AGVM Core is a local-first memory operating system for MCP clients. It gives an
AI client a persistent memory brain, a self-hosted API, a local stdio MCP bridge
and a core UI for setup, inspection, health checks, benchmarks and raw MCP calls.

This repository is the AGPL-3.0 open-source core. It is deliberately smaller than the
private AGVM lab repository: paid modules, cloud control-plane code, private AWS
infrastructure, internal planning documents, local brains and generated
benchmark artifacts are not included.

## What AGVM Core Is

- a local runtime for AGVM memory brains;
- a brain registry and scoped runtime storage;
- public MCP contracts for local AI clients;
- a Docker-based API and UI package;
- setup surfaces for provider keys and MCP client configuration;
- Retrieve, Health, Bench and MCP Raw Console surfaces;
- public raw MCP primitives for Grow, Sleep, Evolve, Matrix, memory write and
  route inspection.

## What AGVM Core Is Not

- not the paid Clone App, Grow Studio, Maintain Studio or advanced chat product;
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

See [Local Install](docs/local-install.md) for the full setup flow.

## Connect An MCP Client

Start the AGVM API first, then configure your AI app to launch the local stdio
bridge from this checkout:

- [Codex](docs/mcp-codex.md)
- [Claude](docs/mcp-claude.md)
- [Cursor](docs/mcp-cursor.md)

After the app restarts, ask it to call `get_agvm_usage_guide` and list the AGVM
tools it can see. That is the first connection check.

## Core UI

The public Docker UI currently boots a local launch shell that checks API
health and points to the MCP/setup docs. The full core cockpit should expose:

- Retrieve: run or inspect memory retrieval calls when the API exposes the
  retrieve endpoints;
- Health: inspect brain readiness and maintenance signals;
- Bench: review scoped benchmark evidence and claim boundaries;
- MCP Setup: generate client configuration for Codex, Claude, Cursor or generic
  MCP clients;
- MCP Raw Console: call exposed MCP tools directly for debugging.

If a screen shows a disabled or missing rich workflow, treat that as a product
boundary signal. The raw MCP primitives remain part of core.

## Modules And Pro

Advanced modules are not distributed with AGVM Core. The first paid path is
cloud-only: users who want Clone App, Grow Studio, Maintain Studio or advanced
product surfaces use Detwin Cloud with an account-gated cloud brain. Local paid
module downloads are arriving later and are not part of this repository.

See [Modules](docs/modules.md) and [Cloud And Pro](docs/cloud-and-pro.md).

## Privacy And Security

- [Local Vs Cloud Privacy](docs/privacy-local-vs-cloud.md)
- [Security Model](docs/security-model.md)
- [Security Policy](SECURITY.md)
- [License And Notices](docs/license-and-notices.md)
- [Third-Party Notices](THIRD_PARTY_NOTICES.md)

Local mode keeps brains and provider configuration on the user's machine unless
the user explicitly exports data or connects to hosted AGVM.

## Contributing

Read [Contributing](CONTRIBUTING.md) before opening pull requests. Public
contributions must stay inside the core/runtime/docs boundary and must not add
paid module implementations or private platform code.
