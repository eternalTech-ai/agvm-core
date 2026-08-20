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

The public Core UI may show these module slots, but it must route users to
Detwin Cloud instead of installing or serving paid module source locally.

For the public core repository, module code is not copied. Only public contracts
and generic placeholders are allowed.

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

1. the user logs into the hosted AGVM platform;
2. the platform decides whether the account has AGVM Pro;
3. Cloud AGVM checks provider, brain, entitlement and credits;
4. the cloud module action runs only after quota preflight;
5. the platform records a usage event and receipt.

The public core repository contains docs and public contracts needed to explain
this boundary, not the paid implementation.
