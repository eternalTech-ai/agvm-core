<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Cloud And Pro

AGVM Core is local-first and does not require an AGVM cloud account. The planned
hosted AGVM platform is a separate product surface for users who want managed
memory, hosted MCP, usage metering and Pro cloud module activation.

## Local Free Core

Local free core runs on the user's machine:

- local Docker API and UI;
- local brain registry and volumes;
- local stdio MCP bridge;
- local provider key configuration;
- no AGVM cloud login required.

## Hosted AGVM

Hosted AGVM is intended to provide:

- hosted MCP access without running the local product;
- managed tenant/workspace storage;
- account login and billing;
- usage/token metering;
- Cloud-only Pro module entitlements;
- optional cloud persistence for users who explicitly choose hosted mode.

Hosted mode must be tenant-scoped and server-authorized. A hosted token must not
grant access to another user's brain, module entitlement or usage records.

## Detwin Pro

The planned first paid package is a Detwin Pro bundle. It unlocks advanced
modules in Detwin Cloud while keeping the local Core repository free of paid
module source:

- Clone Chat and Teach in Cloud AGVM;
- Maintain Health, Sleep, Evolve and Matrix in Cloud AGVM;
- Hosted MCP for account-scoped cloud brains;
- metered Cloud AGVM actions and receipts.

Local AGVM does not download Clone, Teach or Maintain modules in this release.
It keeps Grow local and routes advanced module calls to Detwin Cloud.

## What Is Public Here

This repository may include:

- public contracts for module manifests and cloud readiness state;
- local core placeholders and module slots;
- documentation explaining how activation works at a high level;
- SDK-facing interfaces that do not expose private platform implementation.

This repository must not include:

- Stripe secret keys or billing implementation;
- private platform API code;
- private AWS infrastructure;
- paid module source code;
- customer data, tenant data or local brain exports.

## Current Status

Cloud and Pro are separate from a local AGVM Core install. If a public core
screen references hosted or Pro capabilities, it should make clear whether the
feature requires platform login, a Pro plan, provider readiness, a cloud brain
or available credits.
