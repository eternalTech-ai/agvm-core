<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Modules

AGVM Core is open source. Optional modules add richer product workflows without
being bundled into the public core repository.

Local Core includes Grow. Advanced Detwin modules are Cloud-only for this
release:

- Clone App;
- Teach;
- Maintain Studio;
- advanced chat/product surfaces.

The public Core UI may show Cloud module entry points, but it must route users
to Detwin Cloud instead of installing or serving paid module source locally.

The local MCP bridge keeps the complete current contract catalog discoverable
subject to client permission policy. This is for AI-client planning, not for
entitlement. Paid-module calls
that require a paid module never run against the local API. Without a Hosted MCP
key or active module lease they return
`module_tool_not_enabled_by_local_mcp_lease` with a structured Detwin Cloud
action contract. If Cloud authentication is missing, the hosted handoff returns
`detwin_cloud_auth_required`. With a valid key, the certified
`sleep_preview` and `evolve_preview` calls are forwarded to Detwin Hosted MCP,
where account, plan, brain, provider and quota checks happen before execution.
Platform settles the dynamic usage reported by the Core terminal receipt. Other
paid tools remain visible and return a structured unavailable result until their
Hosted adapter is certified. Grow remains local and free.

For the public core repository, module code is not copied. Only public contracts
and generic placeholders are allowed.

This includes Brain Profile activation/runtime/benchmark code and Geometry
Calibration apply/rollback code. Public API compatibility routes are
fail-closed Cloud action stubs; they never mutate local nodes or settle local
credits.

## SDK Contracts

The public core export includes SDK contract packages under:

- `sdk/python/agvm_sdk`;
- `sdk/typescript/src`.

New modules should use those contracts for manifests, release grants, safe
account views, MCP tool metadata and UI slots. Existing compatibility adapters
remain in the core API and cockpit UI, but modules should not import private
core internals directly.

## Expected Module Boundary

A module is a cloud capability or separate service that exposes a manifest,
health state and UI entry point through public contracts. Core does not import
paid module source code, and hiding a UI route is not considered an entitlement
boundary. Detwin Cloud must still validate account, plan, credits and runtime
readiness server-side.

## Local And Cloud Activation

The target release model is:

1. the user logs into Detwin;
2. the platform decides whether the account has AGVM Pro;
3. Cloud AGVM checks provider, brain, entitlement and credits;
4. the cloud module action runs only after quota preflight;
5. the platform records a usage event and receipt.

The public core repository contains docs and public contracts needed to explain
this boundary, not the paid implementation.
