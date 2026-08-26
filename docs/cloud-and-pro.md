<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Cloud And Pro

AGVM Core is local-first and does not require a Detwin cloud account. Detwin
Cloud is a separate product surface for users who want managed
memory, hosted MCP, usage metering and Pro cloud module activation.

## Local Free Core

Local free core runs on the user's machine:

- local Docker API and UI;
- local brain registry and volumes;
- local stdio MCP bridge;
- local provider key configuration;
- no AGVM cloud login required.

## Hosted AGVM

Detwin Cloud provides the account and authorization boundary for:

- hosted MCP access without running the local product;
- managed tenant/workspace storage;
- account login and billing;
- usage/token metering;
- Cloud-only Pro module entitlements;
- optional cloud persistence for users who explicitly choose hosted mode.

Hosted mode must be tenant-scoped and server-authorized. A hosted token must not
grant access to another user's brain, module entitlement or usage records.

## Detwin Pro And Pro Plus

Paid Detwin plans unlock advanced Cloud modules while keeping the local Core
repository free of paid module source. The current capability boundary is:

- Detwin Pro: Maintain Health, Sleep, Evolve and Geometry Calibration, Hosted
  MCP for account-scoped cloud brains, and metered Cloud AGVM actions;
- Detwin Pro Plus: every Pro capability plus Clone Chat and Teach.

Local AGVM does not download Clone, Teach or Maintain runtime source. Grow is a
free Core capability. The local MCP bridge nevertheless exposes the public
contract catalog so an AI client can discover the complete workflow. A paid-tool
call without valid module access is blocked before local API execution and
returns a structured action contract directing the user to Detwin Cloud. Tool
visibility is never treated as authorization.

Local Core operations do not consume Detwin credits. Hosted MCP and advanced
Cloud actions perform server-side entitlement and quota preflight, then record
usage only in the hosted platform.

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

Cloud and Pro remain separate from a local AGVM Core install. Public screens and
MCP block responses identify when a capability requires platform login, a Pro
plan, provider readiness, a cloud brain or available credits. The public repo
contains the contracts and recovery guidance, not the paid implementation.
